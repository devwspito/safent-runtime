"""T-G1 🔒 — G1: toda capacidad os_native pasa por el broker (CTRL-P2-1).

Verifica POR AUSENCIA que ningún handler ejecuta el executor nativo sin
pasar por CapabilityBroker.dispatch. Las skills screenshot y screen_record
deben producir un AuditEntry PROPOSAL_EXECUTED cada vez que se invocan,
como evidencia de que el broker (con consent+HITL+kill-switch) estuvo en
la cadena de ejecución (FR-002/FR-005/SC-004/G1).

Tests-first: FALLAN antes de CTRL-P2-1, PASAN después.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from dataclasses import dataclass, field
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
# Helpers: fake os_native executor + dispatcher for tests
# ---------------------------------------------------------------------------


class _FakeOsNativeDispatcher:
    """Records calls, returns a fixed result dict."""

    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._result = result or {"ok": True, "path": "/tmp/test.png"}

    async def execute(self, *, skill_name: str, args: dict) -> dict:
        self.calls.append((skill_name, args))
        return self._result

    def supports(self, skill_name: str) -> bool:
        return skill_name in {"screenshot", "screen_record"}


class _InMemoryAuditRepo:
    """Append-only in-memory audit repository for tests."""

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


def _make_broker_with_os_native(
    *,
    os_native_dispatcher: _FakeOsNativeDispatcher | None = None,
    registry: FakeCapabilityRegistry | None = None,
    consent: _FakeConsentManager | None = None,
) -> tuple[CapabilityBroker, _FakeOsNativeDispatcher, _InMemoryAuditRepo]:
    """Build a CapabilityBroker wired with an OsNativeDispatcher."""
    from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
        SurfaceAdapterDispatcher,
    )
    from hermes.agents_os.domain.surface_kind import SurfaceKind

    os_disp = os_native_dispatcher or _FakeOsNativeDispatcher()
    reg = registry or FakeCapabilityRegistry()
    con = consent or _FakeConsentManager()
    gate = FakeApprovalGate()
    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    audit_repo = _InMemoryAuditRepo()
    intent_log = IntentLog()

    # Empty surface adapter dispatcher — os_native path bypasses it
    surface_dispatcher = SurfaceAdapterDispatcher(adapters={})

    broker = CapabilityBroker(
        registry=reg,
        consent_manager=con,
        approval_gate=gate,
        dispatcher=surface_dispatcher,
        signer=signer,
        audit_repo=audit_repo,
        intent_log=intent_log,
        os_native_dispatcher=os_disp,
    )
    return broker, os_disp, audit_repo


def _os_native_binding(
    tool_name: str,
    *,
    risk: RiskLevel = RiskLevel.LOW,
    auto_executable: bool = True,
    required_capability: str | None = "screen",
) -> CapabilityBinding:
    return CapabilityBinding(
        tool_name=tool_name,
        surface_kind=None,
        required_capability=required_capability,
        risk=risk,
        auto_executable=auto_executable,
        executor="os_native",
    )


def _ctx(*, operator_id: UUID | None = None) -> ConsentContext:
    return ConsentContext(
        tenant_id=_TENANT_ID,
        operator_id=operator_id or _OPERATOR_ID,
    )


def _proposal(tool_name: str, params: dict | None = None) -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT_ID,
        entity_id="test",
        entity_type="test",
        parameters=params or {},
        justification="G1 test",
    )


# ---------------------------------------------------------------------------
# G1-A: screenshot → passes through broker → audit entry produced
# ---------------------------------------------------------------------------


class TestScreenshotPassesThroughBroker:
    """screenshot skill: broker pasa consent gate, luego invoca os_native_dispatcher.
    El AuditEntry PROPOSAL_EXECUTED confirma que el broker estuvo en la cadena.
    """

    async def test_screenshot_produces_audit_entry(self) -> None:
        """screenshot ejecutado ⇒ AuditEntry con kind=PROPOSAL_EXECUTED (G1/CTRL-P2-1)."""
        reg = FakeCapabilityRegistry()
        reg.register(_os_native_binding("screenshot", required_capability=None))

        broker, os_disp, audit_repo = _make_broker_with_os_native(registry=reg)

        outcome = await broker.dispatch(_proposal("screenshot"), _ctx())

        assert outcome.status == ExecutionStatus.EXECUTED, outcome.error
        # Os native executor was called exactly once
        assert len(os_disp.calls) == 1
        assert os_disp.calls[0][0] == "screenshot"
        # Audit entry with PROPOSAL_EXECUTED was produced — broker was in the chain
        executed_entries = [
            e for e in audit_repo.entries
            if e.audit_kind == AuditKind.PROPOSAL_EXECUTED
        ]
        assert len(executed_entries) == 1, (
            "screenshot debe producir exactamente 1 AuditEntry PROPOSAL_EXECUTED "
            "— el broker estuvo en la cadena (G1/CTRL-P2-1)"
        )

    async def test_screenshot_result_returned_in_outcome(self) -> None:
        """ExecutionOutcome.result contiene el dict del executor nativo."""
        reg = FakeCapabilityRegistry()
        reg.register(_os_native_binding("screenshot", required_capability=None))
        fake_result = {"ok": True, "path": "/var/lib/hermes/os-skills/screenshot_abc.png"}
        os_disp = _FakeOsNativeDispatcher(result=fake_result)
        broker, _, _ = _make_broker_with_os_native(registry=reg, os_native_dispatcher=os_disp)

        outcome = await broker.dispatch(_proposal("screenshot"), _ctx())

        assert outcome.result == fake_result

    async def test_audit_entry_id_present(self) -> None:
        """ExecutionOutcome.audit_entry_id es no-None (SC-001/G1)."""
        reg = FakeCapabilityRegistry()
        reg.register(_os_native_binding("screenshot", required_capability=None))
        broker, _, _ = _make_broker_with_os_native(registry=reg)

        outcome = await broker.dispatch(_proposal("screenshot"), _ctx())

        assert outcome.audit_entry_id is not None


# ---------------------------------------------------------------------------
# G1-B: screen_record → requires HITL (WRITE_PROPOSAL → HIGH risk)
# ---------------------------------------------------------------------------


class TestScreenRecordRequiresHitl:
    """screen_record (risk=HIGH, auto_executable=False) → PENDING_APPROVAL sin token."""

    async def test_screen_record_without_token_is_pending(self) -> None:
        """screen_record sin HITL token ⇒ PENDING_APPROVAL, executor NOT called."""
        reg = FakeCapabilityRegistry()
        reg.register(_os_native_binding(
            "screen_record",
            risk=RiskLevel.HIGH,
            auto_executable=False,
            required_capability=None,
        ))
        broker, os_disp, _ = _make_broker_with_os_native(registry=reg)

        outcome = await broker.dispatch(
            _proposal("screen_record", {"duration_seconds": 5}),
            _ctx(),
            hitl_approval_token=None,
        )

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(os_disp.calls) == 0, (
            "executor os_native NO debe invocarse sin token HITL (G1/CTRL-P2-1)"
        )


# ---------------------------------------------------------------------------
# G1-C: Kill-switch blocks os_native dispatch (CTRL-P2-6)
# ---------------------------------------------------------------------------


class TestKillSwitchBlocksOsNative:
    """Broker pausado ⇒ ninguna os_native skill se ejecuta (CTRL-P2-6 + Paso 0)."""

    async def test_os_native_blocked_when_paused(self) -> None:
        from hermes.tasks.domain.ports import AgentStatePort

        class _PausedState:
            async def is_paused(self) -> bool:
                return True

        reg = FakeCapabilityRegistry()
        reg.register(_os_native_binding("screenshot", required_capability=None))
        _, os_disp, audit_repo = _make_broker_with_os_native(registry=reg)

        # Rebuild broker with agent_state
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        gate = FakeApprovalGate()
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        intent_log = IntentLog()
        surface_dispatcher = SurfaceAdapterDispatcher(adapters={})
        audit_repo2 = _InMemoryAuditRepo()
        broker = CapabilityBroker(
            registry=reg,
            consent_manager=_FakeConsentManager(),
            approval_gate=gate,
            dispatcher=surface_dispatcher,
            signer=signer,
            audit_repo=audit_repo2,
            intent_log=intent_log,
            os_native_dispatcher=os_disp,
            agent_state=_PausedState(),  # type: ignore[arg-type]
        )

        outcome = await broker.dispatch(_proposal("screenshot"), _ctx())

        assert outcome.status == ExecutionStatus.REJECTED_BY_POLICY
        assert len(os_disp.calls) == 0, (
            "executor os_native NO debe invocarse cuando el agente está pausado"
        )


# ---------------------------------------------------------------------------
# G1-D: Verify by absence — tool_specs.py handler path does not bypass broker
# ---------------------------------------------------------------------------


class TestToolSpecsDoesNotBypassBroker:
    """Verifica POR AUSENCIA que tool_specs.py no invoca EXECUTORS directamente
    en ningún path que no sea un adaptador de broker.

    Analiza estáticamente que la función _default_read_handler en tool_specs.py
    ya no exista como el único punto de dispatch (el broker debe ser el único
    punto). O bien que esté desactivada / delegue al broker.
    """

    _TOOL_SPECS_PATH = (
        Path(__file__).parent.parent.parent
        / "src/hermes/shell_server/os_native_skills/tool_specs.py"
    )

    def test_os_native_executors_not_called_outside_broker(self) -> None:
        """Ningún módulo fuera de capabilities/ llama a EXECUTORS[skill_name]
        directamente en el path caliente de dispatch (verificación estática).

        El patrón prohibido es: importar EXECUTORS y llamarlo con asyncio.to_thread
        fuera del os_native_dispatcher del broker.
        """
        src = self._TOOL_SPECS_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)

        # Buscar llamadas a asyncio.to_thread(executor, ...) en handlers asincrónicos
        # que estén fuera de una clase OsNativeDispatcher
        to_thread_calls_in_handlers: list[int] = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            # The only legitimate handler is inside the os_native dispatcher adapter
            # (which is at infra level). If _default_read_handler still directly
            # calls to_thread(executor, ...) it's the bypass path.
            if node.name == "_default_read_handler":
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "to_thread"
                    ):
                        to_thread_calls_in_handlers.append(
                            getattr(child, "lineno", -1)
                        )

        assert to_thread_calls_in_handlers == [], (
            f"_default_read_handler en tool_specs.py llama asyncio.to_thread directamente "
            f"en las líneas {to_thread_calls_in_handlers}. "
            "Este es el camino de bypass del broker (G1/CTRL-P2-1). "
            "Migrar screenshot/screen_record a la rama os_native del broker."
        )
