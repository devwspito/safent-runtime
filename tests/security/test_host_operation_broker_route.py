"""Security tests: host-operation tools (mouse_move/mouse_click/type_text)
must route through CapabilityBroker.dispatch — no bypass (CTRL-P2-1 / G1).

Coverage:
  H1 — mouse_move (LOW, auto) → EXECUTED, dispatcher called, audit produced.
  H2 — mouse_click (HIGH, no-auto) → PENDING_APPROVAL without HITL token.
  H3 — type_text (HIGH, no-auto) → PENDING_APPROVAL without HITL token.
  H4 — kill-switch blocks input tools (CTRL-12).
  H5 — missing INPUT_CONTROL consent → REJECTED_BY_CONSENT.
  H6 — All three tools have executor='os_native' (static registry check).
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner, AuditKind
from hermes.agents_os.application.consent_manager import (
    Capability,
    ConsentManager,
    ConsentScope,
)
from hermes.capabilities.application.capability_broker import CapabilityBroker
from hermes.capabilities.application.capability_registry import CapabilityRegistry
from hermes.capabilities.application.intent_log import IntentLog
from hermes.capabilities.domain.ports import (
    CapabilityBinding,
    ConsentContext,
    ExecutionStatus,
    RiskLevel,
)
from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
from hermes.domain.proposal import ToolCallProposal

pytestmark = pytest.mark.unit

_SIGNING_KEY = os.urandom(32)
_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _FakeOsNativeDispatcher:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._result = result or {"ok": True}

    async def execute(self, *, skill_name: str, args: dict) -> dict:
        self.calls.append((skill_name, args))
        return self._result

    def supports(self, skill_name: str) -> bool:
        return skill_name in {"mouse_move", "mouse_click", "type_text"}


class _InMemoryAuditRepo:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def append(self, entry: Any) -> None:
        self.entries.append(entry)

    async def head_hash_hex(self) -> str | None:
        return None

    async def load_chain(self, *, tenant_id: UUID | None = None) -> list[Any]:
        return list(self.entries)


class _FakeConsentManager:
    def __init__(self) -> None:
        self.assert_calls: list[tuple] = []
        self.use_calls: list[tuple] = []

    def assert_active(self, *, human_operator_id: UUID, capability: Capability) -> object:
        self.assert_calls.append((human_operator_id, capability))
        return object()

    def use(self, *, human_operator_id: UUID, capability: Capability) -> object:
        self.use_calls.append((human_operator_id, capability))
        return object()


def _make_broker(
    *,
    os_disp: _FakeOsNativeDispatcher | None = None,
    reg: FakeCapabilityRegistry | None = None,
    consent: _FakeConsentManager | None = None,
    agent_state: Any = None,
) -> tuple[CapabilityBroker, _FakeOsNativeDispatcher, _InMemoryAuditRepo]:
    from hermes.capabilities.infrastructure.surface_adapter_dispatcher import SurfaceAdapterDispatcher

    disp = os_disp or _FakeOsNativeDispatcher()
    registry = reg or FakeCapabilityRegistry()
    con = consent or _FakeConsentManager()
    gate = FakeApprovalGate()
    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    audit_repo = _InMemoryAuditRepo()
    intent_log = IntentLog()
    surface_dispatcher = SurfaceAdapterDispatcher(adapters={})

    broker = CapabilityBroker(
        registry=registry,
        consent_manager=con,
        approval_gate=gate,
        dispatcher=surface_dispatcher,
        signer=signer,
        audit_repo=audit_repo,
        intent_log=intent_log,
        os_native_dispatcher=disp,
        agent_state=agent_state,
    )
    return broker, disp, audit_repo


def _binding(
    tool_name: str,
    *,
    risk: RiskLevel = RiskLevel.LOW,
    auto_executable: bool = True,
) -> CapabilityBinding:
    return CapabilityBinding(
        tool_name=tool_name,
        surface_kind=None,
        required_capability=None,  # consent gate bypassed for simplicity
        risk=risk,
        auto_executable=auto_executable,
        executor="os_native",
    )


def _proposal(tool_name: str, params: dict | None = None) -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT_ID,
        entity_id="os_surface",
        entity_type="os_surface",
        parameters=params or {},
        justification="host-operation test",
    )


def _ctx() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID)


# ---------------------------------------------------------------------------
# H1 — mouse_move (LOW, auto) → EXECUTED, dispatcher called
# ---------------------------------------------------------------------------


class TestMouseMovePassesThroughBroker:
    async def test_mouse_move_executed_and_audited(self) -> None:
        reg = FakeCapabilityRegistry()
        reg.register(_binding("mouse_move", risk=RiskLevel.LOW, auto_executable=True))
        broker, disp, audit_repo = _make_broker(reg=reg)

        outcome = await broker.dispatch(
            _proposal("mouse_move", {"x": 100.0, "y": 200.0}), _ctx()
        )

        assert outcome.status == ExecutionStatus.EXECUTED, outcome.error
        assert len(disp.calls) == 1
        assert disp.calls[0][0] == "mouse_move"
        executed = [e for e in audit_repo.entries if e.audit_kind == AuditKind.PROPOSAL_EXECUTED]
        assert len(executed) == 1, "mouse_move must produce 1 PROPOSAL_EXECUTED audit entry (G1)"

    async def test_mouse_move_result_in_outcome(self) -> None:
        reg = FakeCapabilityRegistry()
        reg.register(_binding("mouse_move"))
        fake_result = {"ok": True}
        disp = _FakeOsNativeDispatcher(result=fake_result)
        broker, _, _ = _make_broker(reg=reg, os_disp=disp)

        outcome = await broker.dispatch(_proposal("mouse_move", {"x": 1, "y": 2}), _ctx())
        assert outcome.result == fake_result


# ---------------------------------------------------------------------------
# H2 — mouse_click (HIGH, no-auto) → PENDING_APPROVAL without HITL token
# ---------------------------------------------------------------------------


class TestMouseClickRequiresHitl:
    async def test_mouse_click_pending_without_token(self) -> None:
        reg = FakeCapabilityRegistry()
        reg.register(_binding("mouse_click", risk=RiskLevel.HIGH, auto_executable=False))
        broker, disp, _ = _make_broker(reg=reg)

        outcome = await broker.dispatch(_proposal("mouse_click", {"btn": 0}), _ctx())

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(disp.calls) == 0, "mouse_click executor must NOT be called without HITL token"


# ---------------------------------------------------------------------------
# H3 — type_text (HIGH, no-auto) → PENDING_APPROVAL without HITL token
# ---------------------------------------------------------------------------


class TestTypeTextRequiresHitl:
    async def test_type_text_pending_without_token(self) -> None:
        reg = FakeCapabilityRegistry()
        reg.register(_binding("type_text", risk=RiskLevel.HIGH, auto_executable=False))
        broker, disp, _ = _make_broker(reg=reg)

        outcome = await broker.dispatch(_proposal("type_text", {"text": "hello"}), _ctx())

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(disp.calls) == 0, "type_text executor must NOT be called without HITL token"


# ---------------------------------------------------------------------------
# H4 — kill-switch blocks input tools (CTRL-12)
# ---------------------------------------------------------------------------


class TestKillSwitchBlocksInputTools:
    async def test_kill_switch_blocks_mouse_move(self) -> None:
        class _Paused:
            async def is_paused(self) -> bool:
                return True

        reg = FakeCapabilityRegistry()
        reg.register(_binding("mouse_move"))
        broker, disp, _ = _make_broker(reg=reg, agent_state=_Paused())

        outcome = await broker.dispatch(_proposal("mouse_move", {"x": 0, "y": 0}), _ctx())

        assert outcome.status == ExecutionStatus.REJECTED_BY_POLICY
        assert len(disp.calls) == 0, "kill-switch must prevent executor from running"


# ---------------------------------------------------------------------------
# H5 — INPUT_CONTROL consent required (static registry check)
# ---------------------------------------------------------------------------


class TestInputControlConsentRequired:
    def test_registry_table_requires_input_control_for_mouse_click(self) -> None:
        reg = CapabilityRegistry()
        binding = reg.resolve("mouse_click")
        assert binding is not None
        assert binding.required_capability == Capability.INPUT_CONTROL.value

    def test_registry_table_requires_input_control_for_type_text(self) -> None:
        reg = CapabilityRegistry()
        binding = reg.resolve("type_text")
        assert binding is not None
        assert binding.required_capability == Capability.INPUT_CONTROL.value

    def test_registry_table_requires_input_control_for_mouse_move(self) -> None:
        reg = CapabilityRegistry()
        binding = reg.resolve("mouse_move")
        assert binding is not None
        assert binding.required_capability == Capability.INPUT_CONTROL.value

    async def test_missing_consent_blocks_mouse_move(self) -> None:
        """mouse_move with required_capability=INPUT_CONTROL but no consent → REJECTED."""
        from hermes.agents_os.application.consent_manager import ConsentDenied

        class _DenyingConsentManager:
            def assert_active(self, *, human_operator_id, capability) -> None:
                raise ConsentDenied("INPUT_CONTROL not granted")

            def use(self, *, human_operator_id, capability) -> None:
                pass

        reg = FakeCapabilityRegistry()
        # Register with the real required_capability
        reg.register(CapabilityBinding(
            tool_name="mouse_move",
            surface_kind=None,
            required_capability=Capability.INPUT_CONTROL.value,
            risk=RiskLevel.LOW,
            auto_executable=True,
            executor="os_native",
        ))
        broker, disp, _ = _make_broker(reg=reg, consent=_DenyingConsentManager())

        outcome = await broker.dispatch(_proposal("mouse_move", {"x": 0, "y": 0}), _ctx())
        assert outcome.status == ExecutionStatus.REJECTED_BY_CONSENT
        assert len(disp.calls) == 0


# ---------------------------------------------------------------------------
# H6 — All three input tools registered with executor='os_native'
# ---------------------------------------------------------------------------


class TestInputToolsRegisteredAsOsNative:
    def test_mouse_move_is_os_native(self) -> None:
        reg = CapabilityRegistry()
        b = reg.resolve("mouse_move")
        assert b is not None
        assert b.executor == "os_native"
        assert b.risk == RiskLevel.LOW
        assert b.auto_executable is True

    def test_mouse_click_is_os_native_high(self) -> None:
        reg = CapabilityRegistry()
        b = reg.resolve("mouse_click")
        assert b is not None
        assert b.executor == "os_native"
        assert b.risk == RiskLevel.HIGH
        assert b.auto_executable is False

    def test_type_text_is_os_native_high(self) -> None:
        reg = CapabilityRegistry()
        b = reg.resolve("type_text")
        assert b is not None
        assert b.executor == "os_native"
        assert b.risk == RiskLevel.HIGH
        assert b.auto_executable is False
