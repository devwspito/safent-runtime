"""T030 — Tests gates del CapabilityBroker (CTRL-1..6, 9, 13, 14).

Cubre:
- LOW + auto_executable ejecuta sin token (CTRL-4).
- HIGH sin token ⇒ PENDING_APPROVAL, no ejecuta (CTRL-1).
- Tool desconocido ⇒ REJECTED_BY_POLICY (Constitución IV).
- operator_id None ⇒ REJECTED_BY_CONSENT (CTRL-13).
- consent.assert_active llamado ANTES del replay (CTRL-2/CONSENT-1).
- taint untrusted ⇒ HITL forzado ignora consent (CTRL-5).
- audit deriva del ReplayOutcome, no del narrative (CTRL-9).
- ApiCall + PII ⇒ HITL elevado (CTRL-14).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
from hermes.agents_os.application.consent_manager import (
    Capability,
    ConsentDenied,
    ConsentManager,
    ConsentScope,
)
from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.application.capability_broker import CapabilityBroker
from hermes.capabilities.application.intent_log import IntentLog
from hermes.capabilities.domain.ports import (
    CapabilityBinding,
    ConsentContext,
    ExecutionStatus,
    RiskLevel,
)
from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor
from hermes.domain.proposal import ToolCallProposal

pytestmark = pytest.mark.unit

_SIGNING_KEY = os.urandom(32)
_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()


# ---------------------------------------------------------------------------
# Fakes de apoyo
# ---------------------------------------------------------------------------


@dataclass
class _RecordingAdapter:
    """Surface adapter fake que graba llamadas y devuelve un outcome scriptado."""

    _surface_kind: SurfaceKind = SurfaceKind.FILESYSTEM
    _outcome_status: ReplayStatus = ReplayStatus.EXECUTED_OK
    calls: list[CapturedAction] = field(default_factory=list)

    @property
    def surface_kind(self) -> SurfaceKind:
        return self._surface_kind

    async def capture(self, **_: Any) -> CapturedAction:  # pragma: no cover
        raise NotImplementedError

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        self.calls.append(action)
        return ReplayOutcome(
            action_id=action.action_id,
            status=self._outcome_status,
        )

    def serialize_for_signing(self, action: CapturedAction) -> bytes:  # pragma: no cover
        return b""


class _FakeConsentManager:
    """ConsentManager fake con control fino sobre assert_active."""

    def __init__(self, *, deny: bool = False) -> None:
        self._deny = deny
        self.assert_active_calls: list[tuple[UUID, str]] = []
        self.use_calls: list[tuple[UUID, str]] = []

    def assert_active(
        self,
        *,
        human_operator_id: UUID,
        capability: Capability,
    ) -> object:
        self.assert_active_calls.append((human_operator_id, capability))
        if self._deny:
            raise ConsentDenied(f"consent denied for {capability}")
        return object()  # dummy Consent

    def use(
        self,
        *,
        human_operator_id: UUID,
        capability: Capability,
    ) -> object:
        self.use_calls.append((human_operator_id, capability))
        return object()


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------


def _make_broker(
    *,
    adapter: _RecordingAdapter | None = None,
    registry: FakeCapabilityRegistry | None = None,
    consent: _FakeConsentManager | None = None,
    approval_gate: FakeApprovalGate | None = None,
    anchor: FakeExternalAnchor | None = None,
    surface_kind: SurfaceKind = SurfaceKind.FILESYSTEM,
) -> tuple[CapabilityBroker, _RecordingAdapter, _FakeConsentManager, FakeApprovalGate]:
    adapter = adapter or _RecordingAdapter(_surface_kind=surface_kind)
    reg = registry or FakeCapabilityRegistry()
    con = consent or _FakeConsentManager()
    gate = approval_gate or FakeApprovalGate()
    anch = anchor or FakeExternalAnchor()
    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    intent_log = IntentLog()

    from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
        SurfaceAdapterDispatcher,
    )
    from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
    from pathlib import Path
    import tempfile

    tmp = tempfile.mkdtemp()
    audit_repo = SqliteAuditRepository(db_path=Path(tmp) / "audit.db")

    dispatcher = SurfaceAdapterDispatcher(
        adapters={surface_kind: adapter}
    )
    broker = CapabilityBroker(
        registry=reg,
        consent_manager=con,
        approval_gate=gate,
        dispatcher=dispatcher,
        signer=signer,
        audit_repo=audit_repo,
        intent_log=intent_log,
        anchor=anch,
    )
    return broker, adapter, con, gate


def _proposal(
    *,
    tool_name: str = "read_file",
    parameters: dict[str, Any] | None = None,
    justification: str = "testing",
) -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT_ID,
        entity_id="test-entity",
        entity_type="test",
        parameters=parameters or {"op": "read_file", "path": "/tmp/x.txt"},
        justification=justification,
    )


def _ctx(*, operator_id: UUID | None = _OPERATOR_ID, untrusted: bool = False) -> ConsentContext:
    return ConsentContext(
        tenant_id=_TENANT_ID,
        operator_id=operator_id,
        derived_from_untrusted_content=untrusted,
    )


# ---------------------------------------------------------------------------
# CTRL-4: LOW + auto_executable ejecuta sin token
# ---------------------------------------------------------------------------


class TestLowAutoExecutable:
    async def test_low_auto_executable_executes_without_token(self) -> None:
        """LOW + auto_executable=True NO requiere hitl_approval_token (CTRL-4)."""
        broker, adapter, _con, _gate = _make_broker()
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="read_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=True,
        ))

        broker, adapter, _con, gate = _make_broker(registry=reg)

        proposal = _proposal(tool_name="read_file")
        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)

        assert outcome.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1
        assert gate.register_calls == []  # no pending_approval registrada

    async def test_low_auto_executable_does_not_register_pending(self) -> None:
        """LOW auto-ejecutable no debe crear entrada en approval_gate."""
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="read_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=True,
        ))
        broker, _adapter, _con, gate = _make_broker(registry=reg)

        proposal = _proposal(tool_name="read_file")
        await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)

        assert gate.register_calls == []


# ---------------------------------------------------------------------------
# CTRL-1: HIGH sin token ⇒ PENDING_APPROVAL, no ejecuta
# ---------------------------------------------------------------------------


class TestHighRequiresToken:
    async def test_high_without_token_returns_pending(self) -> None:
        """HIGH sin token ⇒ PENDING_APPROVAL (CTRL-1)."""
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="write_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability=None,
            risk=RiskLevel.HIGH,
            auto_executable=False,
        ))
        broker, adapter, _con, gate = _make_broker(registry=reg)

        proposal = _proposal(tool_name="write_file")
        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(adapter.calls) == 0  # no ejecutó
        assert proposal.proposal_id in gate.register_calls

    async def test_high_with_valid_token_executes(self) -> None:
        """HIGH con token válido ⇒ EXECUTED (CTRL-1)."""
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter

        minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
        proposal = _proposal(tool_name="write_file")
        token = minter.mint(proposal_id=proposal.proposal_id, capability="documents", ttl=300)

        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="write_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability=None,
            risk=RiskLevel.HIGH,
            auto_executable=False,
        ))

        gate = FakeApprovalGate()
        gate._approved[proposal.proposal_id] = token

        broker, adapter, _con, _gate = _make_broker(registry=reg, approval_gate=gate)

        # Need to replace the broker's minter with one that can verify this token
        # The gate.verify_token delegates to the gate itself in FakeApprovalGate
        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=token)

        assert outcome.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1


# ---------------------------------------------------------------------------
# Constitución IV: tool desconocido ⇒ REJECTED_BY_POLICY
# ---------------------------------------------------------------------------


class TestUnknownToolRejected:
    async def test_unknown_tool_rejected_by_policy(self) -> None:
        """Tool no registrado ⇒ REJECTED_BY_POLICY, no ejecuta (Constitución IV)."""
        broker, adapter, _con, gate = _make_broker()  # registry vacío

        proposal = _proposal(tool_name="nonexistent_tool")
        outcome = await broker.dispatch(proposal, _ctx())

        assert outcome.status == ExecutionStatus.REJECTED_BY_POLICY
        assert len(adapter.calls) == 0
        assert gate.register_calls == []


# ---------------------------------------------------------------------------
# CTRL-13: operator_id None ⇒ REJECTED_BY_CONSENT
# ---------------------------------------------------------------------------


class TestOperatorNoneRejected:
    async def test_operator_id_none_rejected_by_consent(self) -> None:
        """operator_id None ⇒ REJECTED_BY_CONSENT para cualquier capability (CTRL-13)."""
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="write_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability="documents",
            risk=RiskLevel.HIGH,
            auto_executable=False,
        ))
        broker, adapter, _con, gate = _make_broker(registry=reg)

        proposal = _proposal(tool_name="write_file")
        ctx_no_op = _ctx(operator_id=None)
        outcome = await broker.dispatch(proposal, ctx_no_op)

        assert outcome.status == ExecutionStatus.REJECTED_BY_CONSENT
        assert len(adapter.calls) == 0  # no ejecutó


# ---------------------------------------------------------------------------
# CTRL-2/CONSENT-1: consent.assert_active llamado antes del replay
# ---------------------------------------------------------------------------


class TestConsentCalledBeforeReplay:
    async def test_consent_assert_active_called_before_replay(self) -> None:
        """consent.assert_active se invoca ANTES de que adapter.replay ejecute (CTRL-2)."""
        call_order: list[str] = []

        class _TrackingAdapter(_RecordingAdapter):
            async def replay(self, action: CapturedAction, **_: Any) -> ReplayOutcome:
                call_order.append("replay")
                return ReplayOutcome(action_id=action.action_id, status=ReplayStatus.EXECUTED_OK)

        class _TrackingConsent(_FakeConsentManager):
            def assert_active(self, *, human_operator_id: UUID, capability: Capability) -> object:
                call_order.append("assert_active")
                return object()

        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="read_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability="documents",
            risk=RiskLevel.LOW,
            auto_executable=True,
        ))
        adapter = _TrackingAdapter(_surface_kind=SurfaceKind.FILESYSTEM)
        consent = _TrackingConsent()
        broker, _a, _c, _g = _make_broker(adapter=adapter, registry=reg, consent=consent)

        # Replace broker's consent with our tracking one
        object.__setattr__(broker, "_consent_manager", consent)

        proposal = _proposal(tool_name="read_file")
        await broker.dispatch(proposal, _ctx())

        assert call_order.index("assert_active") < call_order.index("replay"), (
            "assert_active DEBE llamarse antes del replay (CTRL-2/CONSENT-1)"
        )

    async def test_consent_denied_prevents_replay(self) -> None:
        """Si consent.assert_active lanza ConsentDenied ⇒ no ejecuta (CTRL-2)."""
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="read_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability="documents",
            risk=RiskLevel.LOW,
            auto_executable=True,
        ))
        consent = _FakeConsentManager(deny=True)
        broker, adapter, _c, _g = _make_broker(registry=reg, consent=consent)

        proposal = _proposal(tool_name="read_file")
        outcome = await broker.dispatch(proposal, _ctx())

        assert outcome.status == ExecutionStatus.REJECTED_BY_CONSENT
        assert len(adapter.calls) == 0


# ---------------------------------------------------------------------------
# CTRL-5: taint untrusted ⇒ HITL forzado ignora consent amplio
# ---------------------------------------------------------------------------


class TestTaintForcesHitl:
    async def test_untrusted_taint_forces_hitl_on_high(self) -> None:
        """derived_from_untrusted_content=True + HIGH ⇒ PENDING_APPROVAL (CTRL-5)."""
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="write_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability=None,
            risk=RiskLevel.HIGH,
            auto_executable=False,
        ))
        broker, adapter, _con, gate = _make_broker(registry=reg)

        proposal = _proposal(tool_name="write_file")
        ctx_untrusted = _ctx(untrusted=True)
        outcome = await broker.dispatch(proposal, ctx_untrusted, hitl_approval_token=None)

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(adapter.calls) == 0

    async def test_untrusted_taint_forces_hitl_on_low_non_auto(self) -> None:
        """derived_from_untrusted_content=True + LOW no auto_executable ⇒ HITL (CTRL-5)."""
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="write_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=False,
        ))
        broker, adapter, _con, gate = _make_broker(registry=reg)

        proposal = _proposal(tool_name="write_file")
        ctx_untrusted = _ctx(untrusted=True)
        outcome = await broker.dispatch(proposal, ctx_untrusted, hitl_approval_token=None)

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(adapter.calls) == 0

    async def test_untrusted_taint_allows_low_auto_executable(self) -> None:
        """derived_from_untrusted_content=True + LOW + auto_executable ⇒ sigue ejecutando (CTRL-5)."""
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="read_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=True,
        ))
        broker, adapter, _con, gate = _make_broker(registry=reg)

        proposal = _proposal(tool_name="read_file")
        ctx_untrusted = _ctx(untrusted=True)
        outcome = await broker.dispatch(proposal, ctx_untrusted, hitl_approval_token=None)

        assert outcome.status == ExecutionStatus.EXECUTED


# ---------------------------------------------------------------------------
# CTRL-9: audit deriva del ReplayOutcome, no del narrative
# ---------------------------------------------------------------------------


class TestAuditFromReplayOutcome:
    async def test_audit_entry_id_present_on_executed(self) -> None:
        """EXECUTED devuelve audit_entry_id real (CTRL-9)."""
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="read_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=True,
        ))
        broker, _adapter, _con, _gate = _make_broker(registry=reg)

        proposal = _proposal(tool_name="read_file")
        outcome = await broker.dispatch(proposal, _ctx())

        assert outcome.status == ExecutionStatus.EXECUTED
        assert outcome.audit_entry_id is not None, (
            "audit_entry_id debe estar presente tras ejecución real (CTRL-9/SC-001)"
        )

    async def test_rejected_has_no_audit_entry_id(self) -> None:
        """REJECTED no produce audit_entry_id de ejecución."""
        broker, _adapter, _con, _gate = _make_broker()

        proposal = _proposal(tool_name="unknown_tool")
        outcome = await broker.dispatch(proposal, _ctx())

        assert outcome.status == ExecutionStatus.REJECTED_BY_POLICY
        assert outcome.audit_entry_id is None

    async def test_failed_replay_maps_to_failed_status(self) -> None:
        """ReplayStatus.EXECUTED_FAILED ⇒ ExecutionStatus.FAILED."""
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="read_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=True,
        ))
        adapter = _RecordingAdapter(
            _surface_kind=SurfaceKind.FILESYSTEM,
            _outcome_status=ReplayStatus.EXECUTED_FAILED,
        )
        broker, _a, _c, _g = _make_broker(adapter=adapter, registry=reg)

        proposal = _proposal(tool_name="read_file")
        outcome = await broker.dispatch(proposal, _ctx())

        assert outcome.status == ExecutionStatus.FAILED


# ---------------------------------------------------------------------------
# CTRL-14: ApiCall con campos PII ⇒ HITL elevado
# ---------------------------------------------------------------------------


class TestApiCallPiiElevatesHitl:
    async def test_api_call_with_pii_fields_forces_hitl(self) -> None:
        """ApiCall con parámetros de PII ⇒ HITL forzado (CTRL-14)."""
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="api_call",
            surface_kind=SurfaceKind.API_CALL,
            required_capability=None,
            risk=RiskLevel.HIGH,
            auto_executable=False,
        ))
        broker, adapter, _con, gate = _make_broker(registry=reg, surface_kind=SurfaceKind.API_CALL)

        proposal = _proposal(
            tool_name="api_call",
            parameters={
                "method": "POST",
                "url": "https://api.example.com/users",
                "body": {"name": "<PII:name:abc>", "email": "<PII:email:def>"},
            },
        )
        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)

        # ApiCall con PII ⇒ debe terminar en PENDING_APPROVAL sin token
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(adapter.calls) == 0
