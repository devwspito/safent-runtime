"""Security tests: 013-P1 — MCP tool calls routed through CapabilityBroker.

Covers:
  (a) LOW (auto_executable) MCP tool call → EXECUTED + audit PROPOSAL_EXECUTED.
  (b) HIGH (tainted context) MCP tool call → PENDING_APPROVAL (HITL gate).
  (c) Unknown server (registry returns None) → REJECTED_BY_POLICY (fail-closed).
  (d) Kill-switch (agent paused) → REJECTED_BY_POLICY before MCP adapter.
  (e) mcp_adapter not configured → REJECTED_BY_POLICY (Constitución IV).
  (f) Browser's existing MCP path (StdioMcpSession) is unaffected by these changes.
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


class _FakeConsentManager:
    def assert_active(self, *, human_operator_id, capability):
        from dataclasses import dataclass as dc
        from hermes.agents_os.application.consent_manager import ConsentScope

        @dc
        class _Consent:
            scope: ConsentScope = ConsentScope.ONCE
        return _Consent()

    def use(self, *, human_operator_id, capability):
        pass


@dataclass
class _RecordingMcpAdapter:
    """Fake McpSurfaceAdapter that records replay calls."""

    calls: list[Any] = field(default_factory=list)
    fail: bool = False

    @property
    def surface_kind(self):
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        return SurfaceKind.MCP_CALL

    async def capture(self, **_: Any):
        raise NotImplementedError

    async def replay(self, action, *, hitl_approval_token=None, consent_token=None):
        from hermes.agents_os.domain.ports.surface_adapter_port import ReplayOutcome, ReplayStatus
        self.calls.append(action)
        if self.fail:
            return ReplayOutcome(action_id=action.action_id, status=ReplayStatus.EXECUTED_FAILED)
        return ReplayOutcome(
            action_id=action.action_id,
            status=ReplayStatus.EXECUTED_OK,
            result={"data": "mcp_result", "is_external_content": True},
        )

    def serialize_for_signing(self, action) -> bytes:
        return b""


def _build_broker(
    *,
    agent_state=None,
    mcp_adapter=None,
    registry=None,
) -> Any:
    from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
    from hermes.capabilities.application.capability_broker import CapabilityBroker
    from hermes.capabilities.application.intent_log import IntentLog
    from hermes.capabilities.infrastructure.surface_adapter_dispatcher import SurfaceAdapterDispatcher
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
        mcp_adapter=mcp_adapter,
    )
    broker._audit_entries = audit_entries
    return broker


def _mcp_proposal(
    tool_name: str = "mcp__playwright-mcp__resource_list",
    *,
    server_id: str = "00000000-0000-0000-0000-000000000001",
) -> Any:
    from hermes.domain.proposal import ToolCallProposal
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT,
        entity_id=str(_OPERATOR),
        entity_type="mcp",
        parameters={
            "server_id": server_id,
            "tool_name": tool_name.split("__")[-1] if "__" in tool_name else tool_name,
            "args": {"resource": "all"},
        },
        justification="MCP: list resources",
    )


def _clean_consent(tainted: bool = False) -> Any:
    from hermes.capabilities.domain.ports import ConsentContext
    return ConsentContext(
        tenant_id=_TENANT,
        operator_id=_OPERATOR,
        derived_from_untrusted_content=tainted,
    )


# ---------------------------------------------------------------------------
# (a) LOW MCP tool → EXECUTED + audit
# ---------------------------------------------------------------------------


class TestMcpLowToolExecuted:
    @pytest.mark.asyncio
    async def test_low_auto_executable_mcp_tool_executes(self) -> None:
        from hermes.agents_os.application.audit_hash_chain import AuditKind
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.capabilities.domain.ports import (
            CapabilityBinding, ExecutionStatus, RiskLevel,
        )
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry

        registry = FakeCapabilityRegistry()
        registry.register(CapabilityBinding(
            tool_name="mcp__playwright-mcp__resource_list",
            surface_kind=SurfaceKind.MCP_CALL,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=True,
            executor="mcp",
        ))

        mcp_adapter = _RecordingMcpAdapter()
        broker = _build_broker(
            agent_state=_RunningAgentState(),
            mcp_adapter=mcp_adapter,
            registry=registry,
        )

        outcome = await broker.dispatch(_mcp_proposal(), _clean_consent())

        assert outcome.status is ExecutionStatus.EXECUTED, (
            f"MCP LOW tool must execute: {outcome.error}"
        )
        assert outcome.audit_entry_id is not None
        assert len(mcp_adapter.calls) == 1

        audit_kinds = [
            getattr(e, "kind", None) or getattr(e, "audit_kind", None)
            for e in broker._audit_entries
        ]
        assert any(
            k == AuditKind.PROPOSAL_EXECUTED or str(k) == "proposal_executed"
            for k in audit_kinds
        ), f"Expected PROPOSAL_EXECUTED audit, got: {audit_kinds}"


# ---------------------------------------------------------------------------
# (b) Tainted context → HITL gate (PENDING_APPROVAL)
# ---------------------------------------------------------------------------


class TestMcpTaintedContextHitl:
    @pytest.mark.asyncio
    async def test_high_risk_tainted_context_forces_hitl_pending(self) -> None:
        """HIGH risk MCP tool under tainted context → PENDING_APPROVAL (CTRL-5).

        Per provenance_taint.requires_forced_hitl():
          - tainted + HIGH → always HITL (even without auto_executable).
          - tainted + LOW + auto_executable=True → allowed (read is not the damage vector).
        This test uses a HIGH-risk MCP tool to verify the gate.
        """
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.capabilities.domain.ports import (
            CapabilityBinding, ExecutionStatus, RiskLevel,
        )
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry

        registry = FakeCapabilityRegistry()
        # HIGH risk tool (e.g. write/delete operation)
        registry.register(CapabilityBinding(
            tool_name="mcp__playwright-mcp__resource_list",
            surface_kind=SurfaceKind.MCP_CALL,
            required_capability=None,
            risk=RiskLevel.HIGH,
            auto_executable=False,
            executor="mcp",
        ))

        mcp_adapter = _RecordingMcpAdapter()
        broker = _build_broker(
            agent_state=_RunningAgentState(),
            mcp_adapter=mcp_adapter,
            registry=registry,
        )

        tainted_consent = _clean_consent(tainted=True)
        outcome = await broker.dispatch(
            _mcp_proposal(), tainted_consent, hitl_approval_token=None
        )

        assert outcome.status is ExecutionStatus.PENDING_APPROVAL, (
            "HIGH risk MCP tool under tainted context must require HITL (CTRL-5)"
        )
        assert len(mcp_adapter.calls) == 0, "Adapter must NOT be called without HITL approval"

    @pytest.mark.asyncio
    async def test_low_auto_exec_tainted_context_allowed(self) -> None:
        """LOW + auto_executable MCP tool under tainted context is allowed.

        Per provenance_taint.requires_forced_hitl(): reading from untrusted
        content is safe; acting on it is the damage vector. LOW+auto_executable
        means read-only, so the broker permits it even under taint.
        """
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.capabilities.domain.ports import (
            CapabilityBinding, ExecutionStatus, RiskLevel,
        )
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry

        registry = FakeCapabilityRegistry()
        registry.register(CapabilityBinding(
            tool_name="mcp__playwright-mcp__resource_list",
            surface_kind=SurfaceKind.MCP_CALL,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=True,
            executor="mcp",
        ))

        mcp_adapter = _RecordingMcpAdapter()
        broker = _build_broker(
            agent_state=_RunningAgentState(),
            mcp_adapter=mcp_adapter,
            registry=registry,
        )

        tainted_consent = _clean_consent(tainted=True)
        outcome = await broker.dispatch(
            _mcp_proposal(), tainted_consent, hitl_approval_token=None
        )

        assert outcome.status is ExecutionStatus.EXECUTED, (
            "LOW+auto_executable MCP read under taint is allowed — the read itself is not damage"
        )


# ---------------------------------------------------------------------------
# (c) Unknown server (registry returns None) → REJECTED_BY_POLICY
# ---------------------------------------------------------------------------


class TestMcpUnknownServerFailClosed:
    @pytest.mark.asyncio
    async def test_unknown_server_rejected_by_policy(self) -> None:
        from hermes.capabilities.domain.ports import ExecutionStatus
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry

        # Empty registry → tool not registered → None
        broker = _build_broker(
            agent_state=_RunningAgentState(),
            mcp_adapter=_RecordingMcpAdapter(),
            registry=FakeCapabilityRegistry(),
        )

        outcome = await broker.dispatch(
            _mcp_proposal("mcp__no-such-server__some_tool"), _clean_consent()
        )
        assert outcome.status is ExecutionStatus.REJECTED_BY_POLICY, (
            "Unknown server must fail-closed: REJECTED_BY_POLICY"
        )


# ---------------------------------------------------------------------------
# (d) Kill-switch (agent paused) → REJECTED_BY_POLICY
# ---------------------------------------------------------------------------


class TestMcpKillSwitch:
    @pytest.mark.asyncio
    async def test_paused_agent_rejects_mcp_tool(self) -> None:
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.capabilities.domain.ports import (
            CapabilityBinding, ExecutionStatus, RiskLevel,
        )
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry

        registry = FakeCapabilityRegistry()
        registry.register(CapabilityBinding(
            tool_name="mcp__playwright-mcp__resource_list",
            surface_kind=SurfaceKind.MCP_CALL,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=True,
            executor="mcp",
        ))

        mcp_adapter = _RecordingMcpAdapter()
        broker = _build_broker(
            agent_state=_PausedAgentState(),
            mcp_adapter=mcp_adapter,
            registry=registry,
        )

        outcome = await broker.dispatch(_mcp_proposal(), _clean_consent())
        assert outcome.status is ExecutionStatus.REJECTED_BY_POLICY
        assert len(mcp_adapter.calls) == 0, "Adapter must NOT be called when agent is paused"


# ---------------------------------------------------------------------------
# (e) mcp_adapter not configured → REJECTED_BY_POLICY
# ---------------------------------------------------------------------------


class TestMcpAdapterNotConfigured:
    @pytest.mark.asyncio
    async def test_no_mcp_adapter_fail_closed(self) -> None:
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.capabilities.domain.ports import (
            CapabilityBinding, ExecutionStatus, RiskLevel,
        )
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry

        registry = FakeCapabilityRegistry()
        registry.register(CapabilityBinding(
            tool_name="mcp__playwright-mcp__resource_list",
            surface_kind=SurfaceKind.MCP_CALL,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=True,
            executor="mcp",
        ))

        broker = _build_broker(
            agent_state=_RunningAgentState(),
            mcp_adapter=None,  # not configured
            registry=registry,
        )

        outcome = await broker.dispatch(_mcp_proposal(), _clean_consent())
        assert outcome.status is ExecutionStatus.REJECTED_BY_POLICY, (
            "mcp_adapter=None must fail-closed (Constitución IV)"
        )
        assert outcome.error is not None
        assert "fail-closed" in outcome.error


# ---------------------------------------------------------------------------
# (f) Browser's existing MCP path (StdioMcpSession) is unaffected
# ---------------------------------------------------------------------------


class TestBrowserMcpPathUnaffected:
    """Verify StdioMcpSession still imports and has its contract unchanged."""

    def test_stdio_mcp_session_importable(self) -> None:
        """browser/infrastructure/mcp_session.py must import without error."""
        from hermes.browser.infrastructure.mcp_session import (
            McpNotInstalledError,
            McpServerConnectionError,
            StdioMcpSession,
        )
        assert StdioMcpSession is not None

    def test_stdio_mcp_session_default_command(self) -> None:
        """Default server_command must remain @playwright/mcp (byte-for-byte same)."""
        from hermes.browser.infrastructure.mcp_session import StdioMcpSession

        session = StdioMcpSession()
        assert session._server_command == ["npx", "@playwright/mcp", "--headless"], (
            "browser's StdioMcpSession default command must be byte-for-byte unchanged"
        )

    def test_stdio_mcp_session_has_browser_methods(self) -> None:
        """navigate, snapshot, click, type_, press, current_url, screenshot still exist."""
        from hermes.browser.infrastructure.mcp_session import StdioMcpSession

        for method in ("navigate", "snapshot", "click", "type_", "press", "current_url", "screenshot"):
            assert hasattr(StdioMcpSession, method), (
                f"StdioMcpSession.{method}() must remain (browser path unchanged)"
            )

    def test_new_stdio_mcp_client_is_separate_class(self) -> None:
        """StdioMcpClient is a distinct class from StdioMcpSession (SRP)."""
        from hermes.browser.infrastructure.mcp_session import StdioMcpSession
        from hermes.mcp.infrastructure.stdio_mcp_client import StdioMcpClient

        assert StdioMcpClient is not StdioMcpSession
        # StdioMcpClient does NOT have browser-specific methods
        assert not hasattr(StdioMcpClient, "navigate")
        assert not hasattr(StdioMcpClient, "snapshot")
