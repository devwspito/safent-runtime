"""Security tests: KC-4 — Composio READ actions routed through CapabilityBroker.

Covers the three required test scenarios:
  (a) Composio READ executed → audit entry PROPOSAL_EXECUTED + kill-switch aborts.
  (b) EXPORT/DOWNLOAD → WRITE_PROPOSAL (no READ auto). [Pre-existing Fix-4, verified here]
  (c) Result of Composio READ marks cycle taint (ingested_untrusted_content=True).

Also covers:
  (d) ComposioSurfaceAdapter.replay routes to ComposioClient.execute_action.
  (e) Broker rejects Composio READ when agent_state.is_paused() (kill-switch).
  (f) ComposioCapabilityRegistry resolves Composio slugs dynamically.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.security

_TENANT = uuid4()
_OPERATOR = uuid4()
_SIGNING_KEY = os.urandom(32)


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _PausedAgentState:
    async def is_paused(self) -> bool:
        return True


class _RunningAgentState:
    async def is_paused(self) -> bool:
        return False


@dataclass
class _RecordingAdapter:
    """Surface adapter fake that records replay calls."""

    calls: list[Any] = field(default_factory=list)

    @property
    def surface_kind(self):
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        return SurfaceKind.FILESYSTEM

    async def capture(self, **_: Any):
        raise NotImplementedError

    async def replay(self, action, *, hitl_approval_token=None, consent_token=None):
        from hermes.agents_os.domain.ports.surface_adapter_port import ReplayOutcome, ReplayStatus
        self.calls.append(action)
        return ReplayOutcome(action_id=action.action_id, status=ReplayStatus.EXECUTED_OK)

    def serialize_for_signing(self, action) -> bytes:
        return b""


class _FakeConsentManager:
    def assert_active(self, *, human_operator_id, capability):
        from hermes.agents_os.application.consent_manager import ConsentScope
        from dataclasses import dataclass as dc

        @dc
        class _Consent:
            scope: ConsentScope = ConsentScope.ONCE
        return _Consent()

    def use(self, *, human_operator_id, capability):
        pass


def _build_broker(
    *,
    agent_state=None,
    composio_adapter=None,
    registry=None,
) -> Any:
    """Build a minimal CapabilityBroker for testing Composio READ routing."""
    from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
    from hermes.capabilities.application.capability_broker import CapabilityBroker
    from hermes.capabilities.application.intent_log import IntentLog
    from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
        SurfaceAdapterDispatcher,
    )
    from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
    from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
    from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor

    if registry is None:
        registry = FakeCapabilityRegistry()

    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    audit_entries: list[Any] = []

    class _InMemoryAuditRepo:
        async def append(self, entry: Any) -> None:
            audit_entries.append(entry)

        async def head_hash_hex(self) -> str | None:
            return None

        async def load_chain(self, *, tenant_id=None):
            return list(audit_entries)

    broker = CapabilityBroker(
        registry=registry,
        consent_manager=_FakeConsentManager(),
        approval_gate=FakeApprovalGate(),
        dispatcher=SurfaceAdapterDispatcher(adapters={}),
        signer=signer,
        audit_repo=_InMemoryAuditRepo(),
        intent_log=IntentLog(),
        anchor=FakeExternalAnchor(),
        agent_state=agent_state,
        composio_adapter=composio_adapter,
    )
    broker._audit_repo_ref = _InMemoryAuditRepo  # keep ref for assertions
    broker._audit_entries = audit_entries
    return broker


# ---------------------------------------------------------------------------
# (a) Composio READ → audit PROPOSAL_EXECUTED + kill-switch aborts
# ---------------------------------------------------------------------------


class TestComposioReadAuditAndKillSwitch:
    """KC-4 (a): READ executed → PROPOSAL_EXECUTED in audit; paused → aborted."""

    @pytest.mark.asyncio
    async def test_composio_read_produces_proposal_executed_audit(self) -> None:
        """Composio READ via broker creates a PROPOSAL_EXECUTED audit entry."""
        from unittest.mock import AsyncMock, patch

        from hermes.agents_os.application.audit_hash_chain import AuditKind
        from hermes.capabilities.application.composio_capability_registry import (
            ComposioCapabilityRegistry,
        )
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import ConsentContext, ExecutionStatus
        from hermes.capabilities.infrastructure.composio_surface_adapter import (
            ComposioSurfaceAdapter,
        )
        from hermes.capabilities.testing.fake_capability_registry import (
            FakeCapabilityRegistry,
        )
        from hermes.domain.proposal import ToolCallProposal

        # ComposioCapabilityRegistry wrapping the static registry
        composio_registry = ComposioCapabilityRegistry(
            static_registry=CapabilityRegistry()
        )

        # Mock ComposioSurfaceAdapter to return success without real network call
        adapter = ComposioSurfaceAdapter(api_key="csk-test", entity_id="ent-1")

        with patch.object(
            adapter,
            "_execute",
            new=AsyncMock(return_value=_ok_replay("act-1")),
        ):
            broker = _build_broker(
                agent_state=_RunningAgentState(),
                composio_adapter=adapter,
                registry=composio_registry,
            )

            proposal = ToolCallProposal(
                proposal_id=uuid4(),
                tool_name="gmail_get_email",
                tenant_id=_TENANT,
                entity_id="ent-1",
                entity_type="composio",
                parameters={
                    "slug": "GMAIL_GET_EMAIL",
                    "params": {"email_id": "msg-001"},
                    "entity_id": "ent-1",
                },
                justification="Composio READ: GMAIL_GET_EMAIL",
            )
            consent = ConsentContext(
                tenant_id=_TENANT,
                operator_id=_OPERATOR,
                derived_from_untrusted_content=False,
            )

            outcome = await broker.dispatch(proposal, consent)

        assert outcome.status is ExecutionStatus.EXECUTED, (
            "KC-4 (a): Composio READ via broker debe devolver EXECUTED"
        )
        assert outcome.audit_entry_id is not None, (
            "KC-4 (a): debe existir audit_entry_id (PROPOSAL_EXECUTED firmado)"
        )

        # Verify at least one PROPOSAL_EXECUTED audit entry was created
        audit_kinds = [
            getattr(e, "kind", None) or getattr(e, "audit_kind", None)
            for e in broker._audit_entries
        ]
        assert any(
            k == AuditKind.PROPOSAL_EXECUTED or str(k) == "proposal_executed"
            for k in audit_kinds
        ), (
            f"KC-4 (a): debe existir entrada de audit PROPOSAL_EXECUTED. "
            f"Encontradas: {audit_kinds}"
        )

    @pytest.mark.asyncio
    async def test_composio_read_aborted_when_agent_paused(self) -> None:
        """Kill-switch: broker aborts Composio READ when agent_state.is_paused()."""
        from unittest.mock import AsyncMock, patch

        from hermes.capabilities.application.composio_capability_registry import (
            ComposioCapabilityRegistry,
        )
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import ConsentContext, ExecutionStatus
        from hermes.capabilities.infrastructure.composio_surface_adapter import (
            ComposioSurfaceAdapter,
        )
        from hermes.domain.proposal import ToolCallProposal

        composio_registry = ComposioCapabilityRegistry(
            static_registry=CapabilityRegistry()
        )
        adapter = ComposioSurfaceAdapter(api_key="csk-test", entity_id="ent-1")
        execute_called = []

        async def _spy_execute(*args, **kwargs):
            execute_called.append(True)
            return _ok_replay("act-2")

        with patch.object(adapter, "_execute", new=AsyncMock(side_effect=_spy_execute)):
            broker = _build_broker(
                agent_state=_PausedAgentState(),
                composio_adapter=adapter,
                registry=composio_registry,
            )

            proposal = ToolCallProposal(
                proposal_id=uuid4(),
                tool_name="gmail_get_email",
                tenant_id=_TENANT,
                entity_id="ent-1",
                entity_type="composio",
                parameters={
                    "slug": "GMAIL_GET_EMAIL",
                    "params": {},
                    "entity_id": "ent-1",
                },
                justification="Composio READ: GMAIL_GET_EMAIL",
            )
            consent = ConsentContext(
                tenant_id=_TENANT,
                operator_id=_OPERATOR,
                derived_from_untrusted_content=False,
            )

            outcome = await broker.dispatch(proposal, consent)

        assert outcome.status is ExecutionStatus.REJECTED_BY_POLICY, (
            "KC-4 kill-switch: Composio READ debe abortarse cuando el agente está pausado. "
            f"Obtenido: {outcome.status}"
        )
        assert not execute_called, (
            "KC-4 kill-switch: _execute NO debe llamarse cuando el agente está pausado"
        )


# ---------------------------------------------------------------------------
# (b) EXPORT/DOWNLOAD → WRITE_PROPOSAL (Fix-4, verified in KC-4 context)
# ---------------------------------------------------------------------------


class TestExportDownloadIsWriteProposal:
    """KC-4 (b): EXPORT/DOWNLOAD slugs → WRITE_PROPOSAL, never READ auto.

    Uses the inline _READ_VERBS logic (copied from composio_tool_specs) to
    avoid importing the composio SDK (unavailable in test environment).
    """

    # Read verbs mirror of composio_tool_specs._READ_VERBS — no SDK import needed.
    _READ_VERBS: frozenset[str] = frozenset(
        {
            "GET", "LIST", "FETCH", "SEARCH", "FIND", "READ", "SHOW",
            "VIEW", "QUERY", "DESCRIBE", "RETRIEVE", "CHECK", "PREVIEW",
            "INSPECT", "STATUS", "PING",
        }
    )

    def _classify(self, slug: str) -> Any:
        from hermes.domain.tool_spec import ToolRisk
        parts = slug.upper().split("_")
        if len(parts) >= 2 and parts[1] in self._READ_VERBS:
            return ToolRisk.READ_ONLY
        return ToolRisk.WRITE_PROPOSAL

    def test_export_slug_is_write_proposal(self) -> None:
        from hermes.domain.tool_spec import ToolRisk

        risk = self._classify("GOOGLEDRIVE_EXPORT_FILE")
        assert risk is ToolRisk.WRITE_PROPOSAL, (
            "KC-4 (b): EXPORT debe ser WRITE_PROPOSAL — es vector de exfiltración "
            "y nunca se auto-ejecuta"
        )

    def test_download_slug_is_write_proposal(self) -> None:
        from hermes.domain.tool_spec import ToolRisk

        risk = self._classify("DROPBOX_DOWNLOAD_FILE")
        assert risk is ToolRisk.WRITE_PROPOSAL, (
            "KC-4 (b): DOWNLOAD debe ser WRITE_PROPOSAL"
        )

    def test_export_not_in_source_read_verbs(self) -> None:
        """EXPORT and DOWNLOAD must not appear in _READ_VERBS in composio_tool_specs.py."""
        import pathlib
        import re

        # Read THIS repo's source (portable, relative to this test file) — NOT a
        # hardcoded absolute path to a sibling checkout, which only passed because
        # that sibling happened to exist and would silently test the wrong tree.
        src = (
            pathlib.Path(__file__).resolve().parents[2]
            / "src/hermes/runtime/composio_tool_specs.py"
        ).read_text()

        match = re.search(r"_READ_VERBS\s*=\s*frozenset\s*\(\s*\{([^}]+)\}", src, re.DOTALL)
        assert match, "_READ_VERBS block must exist in composio_tool_specs.py"
        block = match.group(1)
        verbs = frozenset(re.findall(r'"([A-Z]+)"', block))

        assert "EXPORT" not in verbs, (
            "KC-4 (b): EXPORT no debe estar en _READ_VERBS (Fix-4)"
        )
        assert "DOWNLOAD" not in verbs, (
            "KC-4 (b): DOWNLOAD no debe estar en _READ_VERBS (Fix-4)"
        )

    def test_get_verb_remains_read_only(self) -> None:
        from hermes.domain.tool_spec import ToolRisk

        risk = self._classify("GMAIL_GET_EMAIL")
        assert risk is ToolRisk.READ_ONLY


# ---------------------------------------------------------------------------
# (c) Composio READ result marks cycle taint
# ---------------------------------------------------------------------------


class TestComposioReadMarksCycleTaint:
    """KC-4 (c): Composio READ in CapturingToolHost marks ingested_untrusted_content=True."""

    @pytest.mark.asyncio
    async def test_composio_read_via_tool_host_sets_taint_flag(self) -> None:
        """tag='composio' → ingested_untrusted_content=True in CapturedRound."""
        from hermes.domain.tool_spec import ToolRisk, ToolSpec
        from hermes.runtime.tool_host import CapturingToolHost

        composio_result = {"emails": [{"subject": "test", "body": "content"}]}

        async def _broker_handler(params: dict) -> dict:
            return composio_result

        composio_read_spec = ToolSpec(
            name="gmail_get_email",
            description="Get email via broker",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.READ_ONLY,
            entity_type="composio",
            handler=_broker_handler,
            tags=("composio",),  # this tag triggers the taint
        )

        host = CapturingToolHost(specs=(composio_read_spec,), tenant_id=_TENANT)
        call = {
            "id": "c1",
            "type": "function",
            "function": {"name": "gmail_get_email", "arguments": "{}"},
        }

        round_result = await host.process_round([call])

        assert round_result.ingested_untrusted_content is True, (
            "KC-4 (c): tag 'composio' en la ToolSpec debe activar "
            "ingested_untrusted_content=True en CapturedRound"
        )

    @pytest.mark.asyncio
    async def test_composio_read_result_included_in_tool_results(self) -> None:
        """The result from the broker-handler reaches the LLM as a tool result."""
        from hermes.domain.tool_spec import ToolRisk, ToolSpec
        from hermes.runtime.tool_host import CapturingToolHost

        expected_result = {"emails": [{"id": "msg-1"}]}

        async def _broker_handler(params: dict) -> dict:
            return expected_result

        spec = ToolSpec(
            name="gmail_list_emails",
            description="List emails",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.READ_ONLY,
            entity_type="composio",
            handler=_broker_handler,
            tags=("composio",),
        )

        host = CapturingToolHost(specs=(spec,), tenant_id=_TENANT)
        call = {
            "id": "c2",
            "type": "function",
            "function": {"name": "gmail_list_emails", "arguments": "{}"},
        }

        round_result = await host.process_round([call])

        assert len(round_result.tool_results) == 1, "Debe haber un tool_result"
        import json
        content = json.loads(round_result.tool_results[0].content)
        assert content == expected_result, (
            "El resultado del handler (broker) debe llegar al LLM como tool_result"
        )


# ---------------------------------------------------------------------------
# (d) ComposioSurfaceAdapter routes to ComposioClient.execute_action
# ---------------------------------------------------------------------------


class TestComposioSurfaceAdapterExecution:
    """ComposioSurfaceAdapter delegates to ComposioClient.execute_action."""

    @pytest.mark.asyncio
    async def test_replay_calls_execute_action(self) -> None:
        """ComposioSurfaceAdapter.replay delegates to _execute which calls execute_action."""
        from unittest.mock import AsyncMock

        from hermes.agents_os.domain.ports.surface_adapter_port import (
            CapturedAction,
            ReplayStatus,
        )
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.capabilities.infrastructure.composio_surface_adapter import (
            ComposioSurfaceAdapter,
        )

        adapter = ComposioSurfaceAdapter(api_key="csk-test", entity_id="ent-1")
        action = CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.API_CALL,
            intent_desc="Composio READ: GMAIL_GET_EMAIL",
            payload={
                "slug": "GMAIL_GET_EMAIL",
                "params": {"email_id": "msg-001"},
                "entity_id": "ent-1",
            },
        )

        # Patch _execute directly (ComposioClient is lazy-imported inside it
        # and the composio SDK has an incompatible exception module in this env).
        expected_outcome = _ok_replay("act-test")
        adapter._execute = AsyncMock(return_value=expected_outcome)

        outcome = await adapter.replay(action)

        assert outcome.status is ReplayStatus.EXECUTED_OK
        # replay delegates to _execute with the full call contract, including the
        # connected_account_id (None here) that binds the HMAC audit (CTRL-9) to the
        # concrete account that acted. The invariant enforced: replay never bypasses
        # _execute — every Composio I/O goes through the single execution choke-point.
        adapter._execute.assert_called_once_with(
            action.action_id,
            "GMAIL_GET_EMAIL",
            {"email_id": "msg-001"},
            "ent-1",
            connected_account_id=None,
        )

    @pytest.mark.asyncio
    async def test_replay_fail_closed_on_missing_slug(self) -> None:
        from hermes.agents_os.domain.ports.surface_adapter_port import (
            CapturedAction,
            ReplayStatus,
        )
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.capabilities.infrastructure.composio_surface_adapter import (
            ComposioSurfaceAdapter,
        )

        adapter = ComposioSurfaceAdapter(api_key="csk-test", entity_id="ent-1")
        action = CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.API_CALL,
            intent_desc="missing slug",
            payload={"params": {}},  # no slug!
        )

        outcome = await adapter.replay(action)
        assert outcome.status is ReplayStatus.REJECTED_BY_POLICY, (
            "Sin slug en payload → REJECTED_BY_POLICY (fail-closed)"
        )

    @pytest.mark.asyncio
    async def test_replay_fail_closed_on_surface_kind_mismatch(self) -> None:
        from hermes.agents_os.domain.ports.surface_adapter_port import (
            CapturedAction,
            ReplayStatus,
        )
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.capabilities.infrastructure.composio_surface_adapter import (
            ComposioSurfaceAdapter,
        )

        adapter = ComposioSurfaceAdapter(api_key="csk-test", entity_id="ent-1")
        action = CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.FILESYSTEM,  # wrong surface_kind
            intent_desc="wrong surface",
            payload={"slug": "GMAIL_GET_EMAIL", "params": {}},
        )

        outcome = await adapter.replay(action)
        assert outcome.status is ReplayStatus.REJECTED_BY_POLICY


# ---------------------------------------------------------------------------
# (e) ComposioCapabilityRegistry resolves slugs dynamically
# ---------------------------------------------------------------------------


class TestComposioCapabilityRegistry:
    """ComposioCapabilityRegistry resolves Composio READ slugs to LOW bindings."""

    def test_read_slug_resolved_to_low_composio_binding(self) -> None:
        from hermes.capabilities.application.composio_capability_registry import (
            ComposioCapabilityRegistry,
        )
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import RiskLevel

        registry = ComposioCapabilityRegistry(static_registry=CapabilityRegistry())
        binding = registry.resolve("gmail_get_email")

        assert binding is not None, "gmail_get_email debe resolverse"
        assert binding.risk is RiskLevel.LOW
        assert binding.auto_executable is True
        assert binding.executor == "composio"

    def test_write_slug_resolves_to_high_not_auto(self) -> None:
        """WRITE Composio slugs → HIGH + NOT auto-executable (routed to HITL).

        Design evolution (commit 65bc386): a WRITE slug used to return None, which
        structurally blocked the write BEFORE it could reach an HITL card — the agent
        could never send/create/delete on any integration, even with owner approval.
        The sovereign-write model now binds WRITE slugs to risk=HIGH +
        auto_executable=False so the broker REQUIRES HITL (approval card + TOTP) and
        executes only after the owner approves. Security invariant preserved:
        a WRITE is NEVER auto-executed — no auto path exists.
        """
        from hermes.capabilities.application.composio_capability_registry import (
            ComposioCapabilityRegistry,
        )
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import RiskLevel

        registry = ComposioCapabilityRegistry(static_registry=CapabilityRegistry())
        binding = registry.resolve("gmail_send_email")

        assert binding is not None, (
            "WRITE Composio slug debe resolverse a un binding HITL (no None)"
        )
        assert binding.auto_executable is False, (
            "WRITE Composio slug NUNCA debe auto-ejecutarse — exige HITL"
        )
        assert binding.risk is RiskLevel.HIGH, (
            "WRITE Composio slug debe ser HIGH → el broker exige aprobación del dueño"
        )
        assert binding.executor == "composio"

    def test_static_tools_take_priority(self) -> None:
        """Static registry bindings are not overridden by Composio dynamic resolution."""
        from hermes.capabilities.application.composio_capability_registry import (
            ComposioCapabilityRegistry,
        )
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import RiskLevel

        registry = ComposioCapabilityRegistry(static_registry=CapabilityRegistry())
        # 'read_file' is in the static registry
        binding = registry.resolve("read_file")
        assert binding is not None
        assert binding.executor != "composio", (
            "Herramienta en la registry estática no debe ser sobreescrita por Composio"
        )

    def test_export_slug_resolves_to_high_not_auto(self) -> None:
        """KC-4 (b): EXPORT is a WRITE verb → HIGH + NOT auto-executable (HITL only).

        EXPORT/DOWNLOAD are exfiltration vectors: they are classified WRITE, never
        READ, so they never auto-execute. Post-65bc386 the dynamic registry binds them
        to risk=HIGH + auto_executable=False (routed to an owner-approval card) instead
        of returning None. The security invariant is unchanged: no auto path exists.
        """
        from hermes.capabilities.application.composio_capability_registry import (
            ComposioCapabilityRegistry,
        )
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import RiskLevel

        registry = ComposioCapabilityRegistry(static_registry=CapabilityRegistry())
        binding = registry.resolve("googledrive_export_file")

        assert binding is not None
        assert binding.auto_executable is False, (
            "KC-4 (b): EXPORT NUNCA se auto-ejecuta — es vector de exfiltración"
        )
        assert binding.risk is RiskLevel.HIGH
        assert binding.executor == "composio"

    def test_download_slug_resolves_to_high_not_auto(self) -> None:
        """DOWNLOAD is a WRITE verb → HIGH + NOT auto-executable (HITL only)."""
        from hermes.capabilities.application.composio_capability_registry import (
            ComposioCapabilityRegistry,
        )
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.domain.ports import RiskLevel

        registry = ComposioCapabilityRegistry(static_registry=CapabilityRegistry())
        binding = registry.resolve("dropbox_download_file")

        assert binding is not None
        assert binding.auto_executable is False, (
            "DOWNLOAD NUNCA se auto-ejecuta — mismo vector de exfiltración que EXPORT"
        )
        assert binding.risk is RiskLevel.HIGH
        assert binding.executor == "composio"


# ---------------------------------------------------------------------------
# (f) _make_read_handler with broker produces broker-dispatching handler
# ---------------------------------------------------------------------------


class TestComposioReadHandlerWithBroker:
    """_make_broker_read_handler routes Composio READs through broker.dispatch."""

    @pytest.mark.asyncio
    async def test_broker_read_handler_calls_dispatch(self) -> None:
        """Broker-dispatching handler calls broker.dispatch with correct proposal."""
        from hermes.capabilities.domain.ports import (
            ConsentContext,
            ExecutionOutcome,
            ExecutionStatus,
        )
        from hermes.runtime.composio_broker_handler import make_broker_read_handler

        dispatch_calls: list[Any] = []

        class _CapturingBroker:
            async def dispatch(self, proposal, consent, **kwargs) -> ExecutionOutcome:
                dispatch_calls.append((proposal, consent))
                return ExecutionOutcome(
                    proposal_id=proposal.proposal_id,
                    status=ExecutionStatus.EXECUTED,
                    result={"emails": [{"id": "msg-1"}]},
                )

        consent = ConsentContext(
            tenant_id=_TENANT,
            operator_id=_OPERATOR,
            derived_from_untrusted_content=False,
        )

        handler = make_broker_read_handler(
            slug="GMAIL_GET_EMAIL",
            entity_id="ent-1",
            broker=_CapturingBroker(),
            consent_context=consent,
        )

        result = await handler({"email_id": "msg-1"})

        assert len(dispatch_calls) == 1, "broker.dispatch debe ser llamado una vez"
        proposal, passed_consent = dispatch_calls[0]
        assert proposal.tool_name == "gmail_get_email"
        assert proposal.parameters["slug"] == "GMAIL_GET_EMAIL"
        assert passed_consent is consent
        assert result == {"emails": [{"id": "msg-1"}]}

    @pytest.mark.asyncio
    async def test_broker_read_handler_rejected_returns_error_dict(self) -> None:
        """When broker rejects (e.g. kill-switch), handler returns error dict."""
        from hermes.capabilities.domain.ports import (
            ConsentContext,
            ExecutionOutcome,
            ExecutionStatus,
        )
        from hermes.runtime.composio_broker_handler import make_broker_read_handler

        class _PausingBroker:
            async def dispatch(self, proposal, consent, **kwargs) -> ExecutionOutcome:
                return ExecutionOutcome(
                    proposal_id=proposal.proposal_id,
                    status=ExecutionStatus.REJECTED_BY_POLICY,
                    error="agent paused — dispatch blocked by kill-switch (CTRL-12)",
                )

        consent = ConsentContext(
            tenant_id=_TENANT,
            operator_id=_OPERATOR,
            derived_from_untrusted_content=False,
        )
        handler = make_broker_read_handler(
            slug="GMAIL_GET_EMAIL",
            entity_id="ent-1",
            broker=_PausingBroker(),
            consent_context=consent,
        )

        result = await handler({})

        assert "error" in result, "El resultado debe contener 'error' cuando el broker rechaza"
        assert "composio_read_blocked" in result["error"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ok_replay(action_id_str: str) -> Any:
    """Create a successful ReplayOutcome for mocking."""
    from hermes.agents_os.domain.ports.surface_adapter_port import ReplayOutcome, ReplayStatus

    return ReplayOutcome(
        action_id=uuid4(),
        status=ReplayStatus.EXECUTED_OK,
        result={"data": "test_result"},
    )
