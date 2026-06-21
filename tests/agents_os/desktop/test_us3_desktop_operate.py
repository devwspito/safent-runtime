"""T060/T061/T062/T063/T064 — Tests para el increment 3: operar apps de escritorio.

Cubre:
- T060: LibreOfficeUnoSurfaceAdapter implementa SurfaceAdapterPort; UNO no
  disponible → executed_failed (degradación honesta, no excepción incontrolada).
  Surface mismatch → REJECTED_BY_POLICY.
- T061: bindings DESKTOP_APP en capability_registry — riesgo server-side (NUNCA
  por el LLM). WRITE (lo_write_text, lo_save_document) = HIGH → HITL obligatorio.
  READ (lo_open_document, navigate_app, activate_app) = LOW.
- T062: flujo contextual — la única ruta de efecto para DESKTOP_APP pasa por el
  broker → dispatcher → adapter. Tests demuestran que HIGH sin HITL → adapter
  nunca llamado (INV-1 + INV-4).
- T063: InputOwnershipLedger — un solo dueño por superficie; claim de segundo
  dueño → InputOwnershipViolation; release limpia; reconcile borra todo al boot.
- T064 (STRIDE choke-point invariant): el adapter UNO vive en infrastructure; el
  executor server-side es 'surface_adapter' (pasa por dispatcher, no os_native).
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
    SurfaceAdapterPort,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter import (
    LibreOfficeUnoSurfaceAdapter,
    _check_uno_available,
    _find_soffice_binary,
    _kill_lo_process,
    _launch_lo_process,
    _make_profile_dir,
    _parse_cell_address,
    _pipe_name_from_profile,
)

pytestmark = pytest.mark.unit

_SIGNING_KEY = os.urandom(32)
_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _action(
    *,
    surface_kind: SurfaceKind = SurfaceKind.DESKTOP_APP,
    payload: dict[str, Any] | None = None,
) -> CapturedAction:
    return CapturedAction(
        action_id=uuid4(),
        surface_kind=surface_kind,
        intent_desc="test action",
        payload=payload or {},
        tenant_id=_TENANT_ID,
        human_operator_id=_OPERATOR_ID,
    )


def _proposal(
    tool_name: str,
    parameters: dict[str, Any] | None = None,
    justification: str = "testing",
):
    from hermes.domain.proposal import ToolCallProposal
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT_ID,
        entity_id="test-entity",
        entity_type="test",
        parameters=parameters or {},
        justification=justification,
    )


def _consent_ctx(*, tainted: bool = False):
    from hermes.capabilities.domain.ports import ConsentContext
    return ConsentContext(
        tenant_id=_TENANT_ID,
        operator_id=_OPERATOR_ID,
        derived_from_untrusted_content=tainted,
    )


@dataclass
class _RecordingAdapter:
    """Fake SurfaceAdapterPort para tests del broker."""

    _surface_kind: SurfaceKind = SurfaceKind.DESKTOP_APP
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

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        return b""


class _FakeConsentManager:
    def assert_active(self, *, human_operator_id: UUID, capability: Any) -> object:
        return object()

    def use(self, *, human_operator_id: UUID, capability: Any) -> object:
        return object()


def _make_broker(
    *,
    adapter: _RecordingAdapter | None = None,
    surface_kind: SurfaceKind = SurfaceKind.DESKTOP_APP,
) -> tuple[Any, _RecordingAdapter]:
    """Build a CapabilityBroker with a recording DESKTOP_APP adapter."""
    from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
    from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
    from hermes.capabilities.application.capability_broker import CapabilityBroker
    from hermes.capabilities.application.capability_registry import ExtendedCapabilityBinding
    from hermes.capabilities.application.intent_log import IntentLog
    from hermes.capabilities.domain.ports import RiskLevel
    from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
        SurfaceAdapterDispatcher,
    )
    from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
    from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
    from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor

    rec = adapter or _RecordingAdapter(_surface_kind=surface_kind)
    reg = FakeCapabilityRegistry()

    reg.register(
        ExtendedCapabilityBinding(
            tool_name="lo_open_document",
            surface_kind=SurfaceKind.DESKTOP_APP,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=True,
            executor="surface_adapter",
        )
    )
    reg.register(
        ExtendedCapabilityBinding(
            tool_name="lo_write_text",
            surface_kind=SurfaceKind.DESKTOP_APP,
            required_capability=None,
            risk=RiskLevel.HIGH,
            auto_executable=False,
            executor="surface_adapter",
        )
    )
    reg.register(
        ExtendedCapabilityBinding(
            tool_name="lo_save_document",
            surface_kind=SurfaceKind.DESKTOP_APP,
            required_capability=None,
            risk=RiskLevel.HIGH,
            auto_executable=False,
            executor="surface_adapter",
        )
    )

    tmp = tempfile.mkdtemp()
    audit_repo = SqliteAuditRepository(db_path=Path(tmp) / "audit.db")
    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    gate = FakeApprovalGate()
    intent_log = IntentLog()
    anchor = FakeExternalAnchor()
    dispatcher = SurfaceAdapterDispatcher(adapters={surface_kind: rec})

    broker = CapabilityBroker(
        registry=reg,
        consent_manager=_FakeConsentManager(),
        approval_gate=gate,
        dispatcher=dispatcher,
        signer=signer,
        audit_repo=audit_repo,
        intent_log=intent_log,
        anchor=anchor,
    )
    return broker, rec


# ===========================================================================
# T060 — LibreOfficeUnoSurfaceAdapter unit tests
# ===========================================================================


class TestLibreOfficeUnoAdapterContract:
    """T060: adapter implementa SurfaceAdapterPort; fail-closed ante UNO ausente."""

    def test_surface_kind_is_desktop_app(self) -> None:
        adapter = LibreOfficeUnoSurfaceAdapter()
        assert adapter.surface_kind == SurfaceKind.DESKTOP_APP

    def test_implements_surface_adapter_port_protocol(self) -> None:
        """Structural check: adapter cumple SurfaceAdapterPort (runtime_checkable)."""
        adapter = LibreOfficeUnoSurfaceAdapter()
        assert isinstance(adapter, SurfaceAdapterPort)

    @pytest.mark.asyncio
    async def test_surface_kind_mismatch_rejected_by_policy(self) -> None:
        """surface_kind != DESKTOP_APP → REJECTED_BY_POLICY (fail-closed, no UNO llamado)."""
        adapter = LibreOfficeUnoSurfaceAdapter()
        action = _action(surface_kind=SurfaceKind.BROWSER)
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY
        assert "mismatch" in (outcome.error or "").lower()

    @pytest.mark.asyncio
    async def test_unknown_op_rejected_by_policy(self) -> None:
        """Operación no en _ALLOWED_OPS → REJECTED_BY_POLICY."""
        adapter = LibreOfficeUnoSurfaceAdapter()
        action = _action(payload={"op": "delete_all"})
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY
        assert "delete_all" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_uno_not_available_returns_executed_failed_not_exception(self) -> None:
        """Si python3-uno no está disponible, replay() devuelve EXECUTED_FAILED.

        Degradación honesta: el adapter existe y responde, sin lanzar excepción.
        No falla silenciosamente — el error describe el problema claramente.
        """
        adapter = LibreOfficeUnoSurfaceAdapter()
        action = _action(
            payload={
                "op": "open_document",
                "document_path": "/nonexistent/test.odt",
            }
        )

        if _check_uno_available():
            # En un entorno con UNO instalado, la falla es de fichero no encontrado.
            # Verificamos que no lanza excepción incontrolada.
            outcome = await adapter.replay(action)
            assert outcome.status in (ReplayStatus.EXECUTED_FAILED, ReplayStatus.REJECTED_BY_POLICY)
        else:
            # UNO no disponible → EXECUTED_FAILED con mensaje descriptivo.
            outcome = await adapter.replay(action)
            assert outcome.status == ReplayStatus.EXECUTED_FAILED
            assert "python3-uno" in (outcome.error or "").lower()
            assert outcome.action_id == action.action_id

    @pytest.mark.asyncio
    async def test_replay_always_returns_replay_outcome(self) -> None:
        """replay() siempre devuelve ReplayOutcome, nunca lanza."""
        adapter = LibreOfficeUnoSurfaceAdapter()
        action = _action(payload={"op": "write_text"})
        outcome = await adapter.replay(action)
        assert isinstance(outcome, ReplayOutcome)

    def test_serialize_for_signing_is_deterministic(self) -> None:
        """serialize_for_signing devuelve los mismos bytes para la misma acción."""
        adapter = LibreOfficeUnoSurfaceAdapter()
        action = _action(
            payload={"op": "open_document", "document_path": "/docs/test.odt"}
        )
        bytes1 = adapter.serialize_for_signing(action)
        bytes2 = adapter.serialize_for_signing(action)
        assert bytes1 == bytes2
        assert isinstance(bytes1, bytes)
        assert len(bytes1) > 0

    def test_serialize_for_signing_differs_for_different_ops(self) -> None:
        """Distintas operaciones producen bytes distintos (firmable único)."""
        adapter = LibreOfficeUnoSurfaceAdapter()
        action_open = _action(payload={"op": "open_document"})
        action_write = _action(payload={"op": "write_text"})
        assert adapter.serialize_for_signing(action_open) != adapter.serialize_for_signing(action_write)

    @pytest.mark.asyncio
    async def test_capture_returns_captured_action(self) -> None:
        """capture() devuelve CapturedAction con los metadatos correctos."""
        adapter = LibreOfficeUnoSurfaceAdapter()
        action = await adapter.capture(
            intent_desc="Abrir documento",
            params={"op": "open_document", "document_path": "/docs/test.odt"},
            tenant_id=_TENANT_ID,
            human_operator_id=_OPERATOR_ID,
        )
        assert isinstance(action, CapturedAction)
        assert action.surface_kind == SurfaceKind.DESKTOP_APP
        assert action.tenant_id == _TENANT_ID

    @pytest.mark.asyncio
    async def test_unknown_op_via_operation_key_also_rejected(self) -> None:
        """El payload puede usar 'operation' como alias de 'op' — fuera de la lista → REJECTED."""
        adapter = LibreOfficeUnoSurfaceAdapter()
        action = _action(payload={"operation": "rm_rf_slash"})
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY

    @pytest.mark.asyncio
    async def test_empty_op_rejected_by_policy(self) -> None:
        """Payload sin 'op' ni 'operation' → REJECTED_BY_POLICY (fail-closed)."""
        adapter = LibreOfficeUnoSurfaceAdapter()
        action = _action(payload={"document_path": "/docs/test.odt"})
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY


class TestParseCellAddress:
    """Unit tests for _parse_cell_address helper."""

    def test_a1_returns_zero_zero(self) -> None:
        assert _parse_cell_address("A1") == (0, 0)

    def test_b2_returns_one_one(self) -> None:
        assert _parse_cell_address("B2") == (1, 1)

    def test_lowercase_accepted(self) -> None:
        assert _parse_cell_address("a1") == (0, 0)

    def test_no_digits_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            _parse_cell_address("ABC")  # letters only, no row digits

    def test_no_letters_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            _parse_cell_address("123")  # digits only, no column letter

    def test_empty_address_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            _parse_cell_address("")


# ===========================================================================
# T061 — Bindings server-side en capability_registry
# ===========================================================================


class TestDesktopAppBindingsServerSide:
    """T061: riesgo fijado server-side, NUNCA por el LLM (anti prompt-injection)."""

    def _registry(self):
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        return CapabilityRegistry()

    def test_lo_open_document_is_low_risk(self) -> None:
        """Abrir documento = READ = LOW; sin HITL."""
        from hermes.capabilities.domain.ports import RiskLevel
        binding = self._registry().resolve("lo_open_document")
        assert binding is not None
        assert binding.risk == RiskLevel.LOW
        assert binding.auto_executable is True
        assert binding.surface_kind == SurfaceKind.DESKTOP_APP

    def test_lo_write_text_is_high_risk(self) -> None:
        """Escribir en documento = WRITE = HIGH; HITL obligatorio."""
        from hermes.capabilities.domain.ports import RiskLevel
        binding = self._registry().resolve("lo_write_text")
        assert binding is not None
        assert binding.risk == RiskLevel.HIGH
        assert binding.auto_executable is False
        assert binding.surface_kind == SurfaceKind.DESKTOP_APP

    def test_lo_save_document_is_high_risk(self) -> None:
        """Guardar documento = efecto irreversible = HIGH; HITL obligatorio."""
        from hermes.capabilities.domain.ports import RiskLevel
        binding = self._registry().resolve("lo_save_document")
        assert binding is not None
        assert binding.risk == RiskLevel.HIGH
        assert binding.auto_executable is False
        assert binding.surface_kind == SurfaceKind.DESKTOP_APP

    def test_lo_write_persistent_consent_forbidden(self) -> None:
        """lo_write_text: persistent_forbidden=True — consent PERSISTENT inaceptable."""
        binding = self._registry().resolve("lo_write_text")
        assert binding is not None
        assert getattr(binding, "persistent_forbidden", False) is True

    def test_lo_save_persistent_consent_forbidden(self) -> None:
        """lo_save_document: persistent_forbidden=True."""
        binding = self._registry().resolve("lo_save_document")
        assert binding is not None
        assert getattr(binding, "persistent_forbidden", False) is True

    def test_navigate_app_is_low_risk(self) -> None:
        """navigate_app (observar árbol accesibilidad) = LOW."""
        from hermes.capabilities.domain.ports import RiskLevel
        binding = self._registry().resolve("navigate_app")
        assert binding is not None
        assert binding.risk == RiskLevel.LOW

    def test_activate_app_is_low_risk(self) -> None:
        """activate_app (focus sin clic) = LOW."""
        from hermes.capabilities.domain.ports import RiskLevel
        binding = self._registry().resolve("activate_app")
        assert binding is not None
        assert binding.risk == RiskLevel.LOW

    def test_click_app_element_is_high_risk(self) -> None:
        """Clic en elemento de app = HIGH (puede enviar formularios, borrar)."""
        from hermes.capabilities.domain.ports import RiskLevel
        binding = self._registry().resolve("click_app_element")
        assert binding is not None
        assert binding.risk == RiskLevel.HIGH

    def test_type_in_app_is_high_risk(self) -> None:
        """Teclear en app = HIGH (puede ser credencial, comando destructivo)."""
        from hermes.capabilities.domain.ports import RiskLevel
        binding = self._registry().resolve("type_in_app")
        assert binding is not None
        assert binding.risk == RiskLevel.HIGH

    def test_all_desktop_writes_are_high(self) -> None:
        """Todos los tools de escritura en apps son HIGH — invariante de seguridad (INV-4)."""
        from hermes.capabilities.domain.ports import RiskLevel
        registry = self._registry()
        write_tools = ["lo_write_text", "lo_save_document", "click_app_element", "type_in_app"]
        for tool in write_tools:
            binding = registry.resolve(tool)
            assert binding is not None, f"Tool {tool!r} no registrado"
            assert binding.risk == RiskLevel.HIGH, (
                f"Tool {tool!r} debería ser HIGH pero es {binding.risk!r}. "
                "WRITE en app = HIGH es INVARIANTE de seguridad (INV-4)."
            )

    def test_all_desktop_bindings_use_surface_adapter_executor(self) -> None:
        """Todos los tools DESKTOP_APP usan executor='surface_adapter' (INV-1 / T064)."""
        registry = self._registry()
        desktop_tools = [
            "lo_open_document", "lo_write_text", "lo_save_document",
            "navigate_app", "activate_app", "click_app_element", "type_in_app",
        ]
        for tool in desktop_tools:
            binding = registry.resolve(tool)
            assert binding is not None, f"Tool {tool!r} no registrado"
            executor = getattr(binding, "executor", None)
            assert executor == "surface_adapter", (
                f"Tool {tool!r} tiene executor={executor!r} — debe ser 'surface_adapter' "
                "para que el broker use el SurfaceAdapterDispatcher (INV-1)."
            )


# ===========================================================================
# T062 — Flujo contextual: la única ruta de efecto pasa por el broker
# ===========================================================================


class TestContextualOperateFlow:
    """T062: única ruta overlay → broker → dispatcher → adapter (INV-1)."""

    @pytest.mark.asyncio
    async def test_low_risk_desktop_op_reaches_adapter(self) -> None:
        """lo_open_document (LOW) pasa por broker → dispatcher → adapter sin HITL."""
        from hermes.capabilities.domain.ports import ExecutionStatus

        broker, adapter = _make_broker()
        prop = _proposal(
            "lo_open_document",
            parameters={"op": "open_document", "document_path": "/tmp/test.odt"},
        )

        outcome = await broker.dispatch(prop, _consent_ctx())

        assert outcome.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1
        dispatched_action = adapter.calls[0]
        assert dispatched_action.surface_kind == SurfaceKind.DESKTOP_APP

    @pytest.mark.asyncio
    async def test_high_risk_desktop_write_without_hitl_is_pending(self) -> None:
        """lo_write_text (HIGH) sin token HITL → PENDING_APPROVAL; adapter NO llamado (INV-4)."""
        from hermes.capabilities.domain.ports import ExecutionStatus

        broker, adapter = _make_broker()
        prop = _proposal(
            "lo_write_text",
            parameters={
                "op": "write_text",
                "document_path": "/tmp/test.odt",
                "text": "Hello, World!",
            },
        )

        outcome = await broker.dispatch(prop, _consent_ctx())

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(adapter.calls) == 0, (
            f"El adapter fue llamado {len(adapter.calls)} veces — "
            "HIGH sin token HITL NO debe llegar al adapter (INV-4)."
        )

    @pytest.mark.asyncio
    async def test_high_risk_desktop_write_with_hitl_reaches_adapter(self) -> None:
        """lo_write_text (HIGH) con token HITL válido → adapter ejecuta."""
        from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
        from hermes.capabilities.domain.ports import ExecutionStatus
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.capabilities.application.capability_broker import CapabilityBroker
        from hermes.capabilities.application.capability_registry import ExtendedCapabilityBinding
        from hermes.capabilities.application.intent_log import IntentLog
        from hermes.capabilities.domain.ports import RiskLevel
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
        from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor

        gate = FakeApprovalGate(auto_approve=False)
        adapter = _RecordingAdapter(_surface_kind=SurfaceKind.DESKTOP_APP)
        reg = FakeCapabilityRegistry()
        reg.register(
            ExtendedCapabilityBinding(
                tool_name="lo_write_text",
                surface_kind=SurfaceKind.DESKTOP_APP,
                required_capability=None,
                risk=RiskLevel.HIGH,
                auto_executable=False,
                executor="surface_adapter",
            )
        )
        tmp = tempfile.mkdtemp()
        audit_repo = SqliteAuditRepository(db_path=Path(tmp) / "audit.db")
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        dispatcher = SurfaceAdapterDispatcher(adapters={SurfaceKind.DESKTOP_APP: adapter})
        broker = CapabilityBroker(
            registry=reg,
            consent_manager=_FakeConsentManager(),
            approval_gate=gate,
            dispatcher=dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=IntentLog(),
            anchor=FakeExternalAnchor(),
        )

        prop = _proposal(
            "lo_write_text",
            parameters={
                "op": "write_text",
                "document_path": "/tmp/test.odt",
                "text": "agent",
            },
        )

        # First dispatch registers pending approval.
        outcome_pending = await broker.dispatch(prop, _consent_ctx())
        assert outcome_pending.status == ExecutionStatus.PENDING_APPROVAL

        # Human approves via gate.
        token = await gate.approve(proposal_id=prop.proposal_id, approved_by=_OPERATOR_ID)

        # Re-dispatch with valid token → adapter called.
        outcome_exec = await broker.dispatch(prop, _consent_ctx(), hitl_approval_token=token)
        assert outcome_exec.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1

    @pytest.mark.asyncio
    async def test_browser_proposal_does_not_reach_desktop_app_adapter(self) -> None:
        """Una propuesta BROWSER no llega al adapter DESKTOP_APP (fail-closed)."""
        from hermes.capabilities.domain.ports import ExecutionStatus, RiskLevel
        from hermes.capabilities.application.capability_registry import ExtendedCapabilityBinding
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.capabilities.application.capability_broker import CapabilityBroker
        from hermes.capabilities.application.intent_log import IntentLog
        from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
        from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor

        desktop_adapter = _RecordingAdapter(_surface_kind=SurfaceKind.DESKTOP_APP)
        reg = FakeCapabilityRegistry()
        reg.register(
            ExtendedCapabilityBinding(
                tool_name="navigate",
                surface_kind=SurfaceKind.BROWSER,
                required_capability=None,
                risk=RiskLevel.LOW,
                auto_executable=True,
                executor="surface_adapter",
            )
        )

        tmp = tempfile.mkdtemp()
        audit_repo = SqliteAuditRepository(db_path=Path(tmp) / "audit.db")
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)

        # Only DESKTOP_APP adapter registered — no BROWSER adapter.
        dispatcher = SurfaceAdapterDispatcher(
            adapters={SurfaceKind.DESKTOP_APP: desktop_adapter}
        )
        broker = CapabilityBroker(
            registry=reg,
            consent_manager=_FakeConsentManager(),
            approval_gate=FakeApprovalGate(),
            dispatcher=dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=IntentLog(),
            anchor=FakeExternalAnchor(),
        )

        prop = _proposal("navigate", parameters={"url": "https://example.com"})
        outcome = await broker.dispatch(prop, _consent_ctx())

        # No BROWSER adapter → REJECTED_BY_POLICY (fail-closed).
        assert outcome.status == ExecutionStatus.REJECTED_BY_POLICY
        # DESKTOP_APP adapter NOT called.
        assert len(desktop_adapter.calls) == 0


# ===========================================================================
# T063 — InputOwnershipLedger: aislamiento de input
# ===========================================================================


class TestInputOwnershipLedger:
    """T063: un solo dueño por superficie; reconcile limpia (INV-2, FR-021..026)."""

    def _make_registry(self):
        from hermes.execution.application.execution_context_registry import (
            ExecutionContextRegistry,
        )
        return ExecutionContextRegistry()

    def test_claim_succeeds_for_first_owner(self) -> None:
        from hermes.execution.domain.ports import (
            ExecutionContextId,
            InputOwnerKind,
            InputSurfaceKey,
            InputSurfaceKind,
        )
        registry = self._make_registry()
        surface = InputSurfaceKey(kind=InputSurfaceKind.KEYBOARD, surface_id="lo-headless-1")
        owner = ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.AGENT_TASK)
        registry.claim(surface=surface, owner=owner)
        assert registry.owner_of(surface=surface) == owner

    def test_claim_by_second_owner_raises_ownership_violation(self) -> None:
        """Dos dueños distintos en la misma superficie → InputOwnershipViolation (INV-2)."""
        from hermes.execution.domain.ports import (
            ExecutionContextId,
            InputOwnerKind,
            InputOwnershipViolation,
            InputSurfaceKey,
            InputSurfaceKind,
        )
        registry = self._make_registry()
        surface = InputSurfaceKey(kind=InputSurfaceKind.KEYBOARD, surface_id="lo-headless-1")
        agent_owner = ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.AGENT_TASK)
        human_owner = ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.OPERATOR)

        registry.claim(surface=surface, owner=agent_owner)

        with pytest.raises(InputOwnershipViolation):
            registry.claim(surface=surface, owner=human_owner)

    def test_claim_by_same_owner_is_idempotent(self) -> None:
        """Mismo dueño puede reclamar dos veces sin error (retry-safe)."""
        from hermes.execution.domain.ports import (
            ExecutionContextId,
            InputOwnerKind,
            InputSurfaceKey,
            InputSurfaceKind,
        )
        registry = self._make_registry()
        surface = InputSurfaceKey(kind=InputSurfaceKind.MOUSE, surface_id="lo-headless-1")
        owner = ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.AGENT_TASK)

        registry.claim(surface=surface, owner=owner)
        registry.claim(surface=surface, owner=owner)  # must not raise
        assert registry.owner_of(surface=surface) == owner

    def test_release_frees_surface(self) -> None:
        from hermes.execution.domain.ports import (
            ExecutionContextId,
            InputOwnerKind,
            InputSurfaceKey,
            InputSurfaceKind,
        )
        registry = self._make_registry()
        surface = InputSurfaceKey(kind=InputSurfaceKind.SCREEN, surface_id="lo-headless-1")
        owner = ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.AGENT_TASK)

        registry.claim(surface=surface, owner=owner)
        registry.release(surface=surface)
        assert registry.owner_of(surface=surface) is None

    def test_release_free_surface_is_noop(self) -> None:
        """release() de superficie libre no lanza (cleanup-safe, FR-023)."""
        from hermes.execution.domain.ports import InputSurfaceKey, InputSurfaceKind
        registry = self._make_registry()
        surface = InputSurfaceKey(kind=InputSurfaceKind.KEYBOARD, surface_id="lo-headless-1")
        registry.release(surface=surface)  # must not raise

    def test_release_all_for_cleans_all_surfaces_of_owner(self) -> None:
        """release_all_for limpia múltiples superficies del mismo dueño (sin fugas)."""
        from hermes.execution.domain.ports import (
            ExecutionContextId,
            InputOwnerKind,
            InputSurfaceKey,
            InputSurfaceKind,
        )
        registry = self._make_registry()
        owner = ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.AGENT_TASK)
        surfaces = [
            InputSurfaceKey(kind=InputSurfaceKind.KEYBOARD, surface_id="lo-1"),
            InputSurfaceKey(kind=InputSurfaceKind.MOUSE, surface_id="lo-1"),
            InputSurfaceKey(kind=InputSurfaceKind.SCREEN, surface_id="lo-1"),
        ]
        for s in surfaces:
            registry.claim(surface=s, owner=owner)

        released = registry.release_all_for(owner=owner)
        assert released == len(surfaces)
        for s in surfaces:
            assert registry.owner_of(surface=s) is None

    def test_reconcile_clears_all_owners(self) -> None:
        """reconcile() purga TODOS los dueños (daemon restart cleanup, FR-026/SC-010)."""
        from hermes.execution.domain.ports import (
            ExecutionContextId,
            InputOwnerKind,
            InputSurfaceKey,
            InputSurfaceKind,
        )
        registry = self._make_registry()
        owner = ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.AGENT_TASK)
        surfaces = [
            InputSurfaceKey(kind=InputSurfaceKind.KEYBOARD, surface_id="lo-2"),
            InputSurfaceKey(kind=InputSurfaceKind.BROWSER, surface_id="browser-sess-1"),
        ]
        for s in surfaces:
            registry.claim(surface=s, owner=owner)

        purged = registry.reconcile()
        assert purged == len(surfaces)
        for s in surfaces:
            assert registry.owner_of(surface=s) is None

    def test_agent_and_human_surfaces_are_disjoint_by_surface_id(self) -> None:
        """Agente y humano tienen surface_id distintos → no colisionan (INV-2)."""
        from hermes.execution.domain.ports import (
            ExecutionContextId,
            InputOwnerKind,
            InputSurfaceKey,
            InputSurfaceKind,
        )
        registry = self._make_registry()
        human_surface = InputSurfaceKey(kind=InputSurfaceKind.KEYBOARD, surface_id="seat0")
        agent_surface = InputSurfaceKey(kind=InputSurfaceKind.KEYBOARD, surface_id="lo-headless-1")

        human_owner = ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.OPERATOR)
        agent_owner = ExecutionContextId(value=uuid4(), owner_kind=InputOwnerKind.AGENT_TASK)

        registry.claim(surface=human_surface, owner=human_owner)
        registry.claim(surface=agent_surface, owner=agent_owner)  # must not raise

        assert registry.owner_of(surface=human_surface) == human_owner
        assert registry.owner_of(surface=agent_surface) == agent_owner


# ===========================================================================
# T064 — STRIDE choke-point invariant: no bypass del broker
# ===========================================================================


class TestChokePointInvariant:
    """T064: test arquitectónico que verifica el choke-point broker → dispatcher → adapter."""

    def test_adapter_uno_module_path_is_infrastructure_layer(self) -> None:
        """El adapter UNO vive en infrastructure, no en application ni domain."""
        module = LibreOfficeUnoSurfaceAdapter.__module__
        assert "infrastructure" in module, (
            f"LibreOfficeUnoSurfaceAdapter debe vivir en infrastructure, "
            f"pero está en {module!r}. Moverlo cambia la frontera de capas."
        )

    def test_dispatcher_registers_desktop_app_adapter(self) -> None:
        """SurfaceAdapterDispatcher expone DESKTOP_APP — el broker puede alcanzarlo."""
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        adapter = _RecordingAdapter(_surface_kind=SurfaceKind.DESKTOP_APP)
        dispatcher = SurfaceAdapterDispatcher(adapters={SurfaceKind.DESKTOP_APP: adapter})

        registered = dispatcher.registered_kinds()
        assert SurfaceKind.DESKTOP_APP in registered

    def test_broker_routes_desktop_app_via_surface_adapter_executor(self) -> None:
        """Todos los tools DESKTOP_APP tienen executor='surface_adapter' (INV-1 / T064)."""
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        registry = CapabilityRegistry()

        desktop_tools = [
            "lo_open_document", "lo_write_text", "lo_save_document",
            "navigate_app", "activate_app", "click_app_element", "type_in_app",
        ]
        for tool in desktop_tools:
            binding = registry.resolve(tool)
            assert binding is not None, f"Tool {tool!r} no registrado en CapabilityRegistry"
            executor = getattr(binding, "executor", None)
            assert executor == "surface_adapter", (
                f"Tool {tool!r} tiene executor={executor!r} — debe ser 'surface_adapter' "
                "para que el broker use el SurfaceAdapterDispatcher (INV-1)."
            )

    @pytest.mark.asyncio
    async def test_write_without_hitl_never_reaches_adapter(self) -> None:
        """HIGH sin HITL → adapter NUNCA llamado (INV-1 + INV-4).

        Si este test falla, el broker tiene un bypass del HITL gate.
        """
        from hermes.capabilities.domain.ports import ExecutionStatus

        broker, adapter = _make_broker()
        prop = _proposal(
            "lo_write_text",
            parameters={"op": "write_text", "document_path": "/tmp/test.odt", "text": "x"},
            justification="choke-point test",
        )

        outcome = await broker.dispatch(prop, _consent_ctx())

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL, (
            f"HIGH sin token debería ser PENDING_APPROVAL, got {outcome.status!r}. "
            "El broker tiene un bypass del HITL gate (INV-4 violado)."
        )
        assert len(adapter.calls) == 0, (
            f"El adapter fue llamado {len(adapter.calls)} veces sin HITL válido. "
            "CRÍTICO: hay un bypass del broker (INV-1 + INV-4 violados)."
        )

    @pytest.mark.asyncio
    async def test_tainted_context_does_not_block_low_auto_executable(self) -> None:
        """Contexto tainted + LOW + auto_executable=True → se permite (CTRL-5 design).

        CTRL-5 eleva a HITL cuando: HIGH + tainted, o LOW + NOT auto_executable + tainted.
        LOW + auto_executable=True (solo lectura) + tainted → ALLOWED: leer desde
        contenido no confiable es safe; el peligro es actuar a partir de él.
        """
        from hermes.capabilities.domain.ports import ExecutionStatus

        broker, adapter = _make_broker()
        prop = _proposal(
            "lo_open_document",  # LOW + auto_executable=True
            parameters={"op": "open_document", "document_path": "/tmp/test.odt"},
            justification="open from tainted web content",
        )

        outcome = await broker.dispatch(prop, _consent_ctx(tainted=True))

        # LOW + auto_executable=True + tainted → broker allows (no HITL needed).
        assert outcome.status == ExecutionStatus.EXECUTED, (
            f"LOW auto_executable tainted debería ser EXECUTED, got {outcome.status!r}. "
            "CTRL-5 no debe bloquear lecturas auto-ejecutables bajo taint."
        )
        assert len(adapter.calls) == 1

    @pytest.mark.asyncio
    async def test_tainted_context_elevates_high_risk_even_if_attempted(self) -> None:
        """Contexto tainted + HIGH → PENDING_APPROVAL (ya era HIGH, CTRL-5 confirma).

        HIGH sin token es siempre PENDING_APPROVAL con o sin taint.
        Con taint, requires_forced_hitl=True también (redundante para HIGH pero verificado).
        """
        from hermes.capabilities.domain.ports import ExecutionStatus

        broker, adapter = _make_broker()
        prop = _proposal(
            "lo_write_text",  # HIGH, NOT auto_executable
            parameters={"op": "write_text", "document_path": "/tmp/test.odt", "text": "x"},
            justification="write from tainted context",
        )

        outcome = await broker.dispatch(prop, _consent_ctx(tainted=True))

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL, (
            f"HIGH tainted sin token debería ser PENDING_APPROVAL, got {outcome.status!r}."
        )
        assert len(adapter.calls) == 0


# ===========================================================================
# I-1 — Spawn propio: el adapter lanza y mata proceso LO por operación
# ===========================================================================


class TestLoProcessSpawnAndIsolation:
    """I-1: cada operación lanza soffice --headless efímero; fail-closed en spawn.

    Estos tests no requieren LO instalado — sondean el comportamiento del adapter
    ante ausencia de binario (EXECUTED_FAILED) y la invariante de unicidad de pipe.
    """

    @pytest.mark.asyncio
    async def test_soffice_not_found_returns_executed_failed(self) -> None:
        """Si soffice no está en PATH, replay() → EXECUTED_FAILED (degradación honesta)."""
        import unittest.mock as mock

        adapter = LibreOfficeUnoSurfaceAdapter()
        action = _action(
            payload={"op": "open_document", "document_path": "/tmp/test.odt"}
        )

        with mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._find_soffice_binary",
            return_value=None,
        ), mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._check_uno_available",
            return_value=True,
        ):
            outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED
        assert "soffice" in (outcome.error or "").lower()

    @pytest.mark.asyncio
    async def test_lo_process_killed_even_when_op_raises(self) -> None:
        """El proceso LO es matado en el finally aunque la operación falle (no fugas).

        Verifica que _kill_lo_process se llama exactamente una vez incluso cuando
        el cuerpo de la operación lanza una excepción.
        """
        import unittest.mock as mock

        adapter = LibreOfficeUnoSurfaceAdapter()
        action = _action(
            payload={"op": "open_document", "document_path": "/tmp/test.odt"}
        )
        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = None

        with mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._check_uno_available",
            return_value=True,
        ), mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._find_soffice_binary",
            return_value="/usr/bin/soffice",
        ), mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._launch_lo_process",
            return_value=fake_proc,
        ), mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._connect_uno_desktop",
            side_effect=TimeoutError("pipe timeout"),
        ), mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._kill_lo_process",
        ) as mock_kill:
            outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED
        mock_kill.assert_called_once_with(fake_proc)

    def test_pipe_name_is_unique_per_profile_dir(self) -> None:
        """Dos action_ids distintos producen nombres de pipe distintos (INV-2)."""
        action_id_1 = uuid4()
        action_id_2 = uuid4()
        dir_1 = f"/tmp/hermes-lo-{action_id_1}"
        dir_2 = f"/tmp/hermes-lo-{action_id_2}"
        pipe_1 = _pipe_name_from_profile(dir_1)
        pipe_2 = _pipe_name_from_profile(dir_2)
        assert pipe_1 != pipe_2, (
            "INV-2 violado: el mismo pipe name para dos action_ids distintos "
            "permitiría al resolver alcanzar una instancia preexistente."
        )

    def test_pipe_name_is_deterministic(self) -> None:
        """El mismo profile_dir siempre produce el mismo pipe (idempotente)."""
        action_id = uuid4()
        profile_dir = f"/tmp/hermes-lo-{action_id}"
        assert _pipe_name_from_profile(profile_dir) == _pipe_name_from_profile(profile_dir)

    def test_pipe_name_fits_unix_socket_limit(self) -> None:
        """El nombre de pipe tiene ≤31 chars (límite práctico UNO pipe names)."""
        profile_dir = f"/tmp/hermes-lo-{uuid4()}"
        name = _pipe_name_from_profile(profile_dir)
        assert len(name) <= 31, f"pipe name demasiado largo: {name!r} ({len(name)} chars)"

    def test_kill_lo_process_none_is_noop(self) -> None:
        """_kill_lo_process(None) no lanza (cleanup safety cuando spawn falló)."""
        _kill_lo_process(None)  # must not raise

    @pytest.mark.asyncio
    async def test_spawn_oserror_returns_executed_failed_not_exception(self) -> None:
        """Si Popen lanza OSError (ej. binario corrupto), replay() → EXECUTED_FAILED.

        Ninguna excepción se propaga al caller del adapter.
        """
        import unittest.mock as mock

        adapter = LibreOfficeUnoSurfaceAdapter()
        action = _action(
            payload={"op": "open_document", "document_path": "/tmp/test.odt"}
        )

        with mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._check_uno_available",
            return_value=True,
        ), mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._find_soffice_binary",
            return_value="/usr/bin/soffice",
        ), mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._launch_lo_process",
            side_effect=OSError("exec format error"),
        ):
            outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED
        assert isinstance(outcome, ReplayOutcome)


# ===========================================================================
# I-2 — Path allowlist: open_document valida contra allowed_prefixes
# ===========================================================================


class TestPathAllowlist:
    """I-2: document_path se valida contra allowed_prefixes antes de UNO (constitución IV)."""

    @pytest.mark.asyncio
    async def test_path_outside_allowlist_returns_executed_failed(self) -> None:
        """Ruta fuera de allowed_prefixes → EXECUTED_FAILED (fail-closed)."""
        import unittest.mock as mock

        adapter = LibreOfficeUnoSurfaceAdapter(
            allowed_prefixes=("/home/hermes/docs",),
        )
        action = _action(
            payload={
                "op": "open_document",
                "document_path": "/etc/passwd",
            }
        )

        with mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._check_uno_available",
            return_value=True,
        ), mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._find_soffice_binary",
            return_value="/usr/bin/soffice",
        ):
            outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED
        assert "allowlist" in (outcome.error or "").lower() or "constitución" in (outcome.error or "").lower()

    @pytest.mark.asyncio
    async def test_path_traversal_outside_allowlist_blocked(self) -> None:
        """Path con traversal (../../etc/passwd) resuelto y bloqueado por allowlist."""
        import unittest.mock as mock

        adapter = LibreOfficeUnoSurfaceAdapter(
            allowed_prefixes=("/home/hermes/docs",),
        )
        action = _action(
            payload={
                "op": "open_document",
                "document_path": "/home/hermes/docs/../../etc/passwd",
            }
        )

        with mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._check_uno_available",
            return_value=True,
        ), mock.patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter._find_soffice_binary",
            return_value="/usr/bin/soffice",
        ):
            outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED

    def test_assert_path_allowed_passes_within_prefix(self) -> None:
        """Ruta dentro del prefix pasa sin excepción."""
        adapter = LibreOfficeUnoSurfaceAdapter(
            allowed_prefixes=("/tmp",),
        )
        # Must not raise — /tmp/test.odt starts with /tmp
        adapter._assert_path_allowed("/tmp/test.odt")

    def test_assert_path_allowed_blocks_outside_prefix(self) -> None:
        """Ruta fuera del prefix lanza PermissionError."""
        adapter = LibreOfficeUnoSurfaceAdapter(
            allowed_prefixes=("/home/hermes/docs",),
        )
        with pytest.raises(PermissionError, match="allowlist"):
            adapter._assert_path_allowed("/etc/shadow")

    def test_assert_path_allowed_blocks_empty_path(self) -> None:
        """Path vacío lanza PermissionError (fail-closed)."""
        adapter = LibreOfficeUnoSurfaceAdapter(
            allowed_prefixes=("/home/hermes/docs",),
        )
        with pytest.raises(PermissionError):
            adapter._assert_path_allowed("")

    def test_assert_path_allowed_none_prefixes_is_permissive(self) -> None:
        """allowed_prefixes=None no aplica restricción (solo para tests sin FS real)."""
        adapter = LibreOfficeUnoSurfaceAdapter(allowed_prefixes=None)
        adapter._assert_path_allowed("/any/path/at/all")  # must not raise

    def test_prefix_boundary_not_tricked_by_common_prefix(self) -> None:
        """'/home/hermes/docs2' no es subdirectorio de '/home/hermes/docs'."""
        adapter = LibreOfficeUnoSurfaceAdapter(
            allowed_prefixes=("/home/hermes/docs",),
        )
        with pytest.raises(PermissionError):
            adapter._assert_path_allowed("/home/hermes/docs2/secret.odt")
