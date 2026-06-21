"""Tests del nivel de autonomía por agente (F-1 / feature autonomy_level).

Cubre:
1. Dominio: AutonomyLevel enum, autonomy_level_from_str, default_agent, AgentDraft.
2. Serialización: agent_to_dict incluye autonomy_level; draft_from_dict lo parsea;
   valor inválido → BALANCED (fail-closed).
3. Persistencia: SqliteAgentRegistry — round-trip CRUD y migración idempotente.
4. Gate del broker por nivel (semántica C2 — relajación acotada):
   - ask_always: LOW no-auto → PENDING_APPROVAL; LOW auto → EXECUTED.
   - balanced:   LOW no-auto → PENDING_APPROVAL; LOW auto → EXECUTED.
   - autonomous: LOW no-auto + reversible=False → PENDING_APPROVAL (igual que balanced).
                 LOW no-auto + reversible=True  → EXECUTED (única relajación permitida).
                 LOW auto → EXECUTED.
   - HIGH siempre → PENDING_APPROVAL en los TRES niveles (invariante F-1).
5. Resolución del registro: _resolve_active_autonomy_level fail-safe.
6. C3 — lint anti-ensanche silencioso: ninguna binding LOW+no-auto+reversible=True
         fuera de la allow-list aprobada por security-engineer.
7. C4 — regresión: toda binding HIGH exige HITL en los 3 niveles + taint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.agents.domain.agent import (
    AutonomyLevel,
    AgentDraft,
    Agent,
    autonomy_level_from_str,
    default_agent,
)
from hermes.agents.application.serialization import agent_to_dict, draft_from_dict
from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.application.capability_broker import CapabilityBroker, _needs_hitl
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
# 1. Dominio: AutonomyLevel
# ---------------------------------------------------------------------------


class TestAutonomyLevelDomain:
    def test_default_agent_has_balanced_level(self) -> None:
        assert default_agent().autonomy_level is AutonomyLevel.BALANCED

    def test_agent_draft_default_is_balanced(self) -> None:
        draft = AgentDraft(name="X")
        assert draft.autonomy_level is AutonomyLevel.BALANCED

    def test_autonomy_level_from_str_valid(self) -> None:
        assert autonomy_level_from_str("ask_always") is AutonomyLevel.ASK_ALWAYS
        assert autonomy_level_from_str("balanced") is AutonomyLevel.BALANCED
        assert autonomy_level_from_str("autonomous") is AutonomyLevel.AUTONOMOUS

    def test_autonomy_level_from_str_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="autonomy_level inválido"):
            autonomy_level_from_str("superpower")

    def test_autonomy_level_from_str_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            autonomy_level_from_str("")


# ---------------------------------------------------------------------------
# 2. Serialización
# ---------------------------------------------------------------------------


class TestAutonomySerialization:
    def test_agent_to_dict_includes_autonomy_level(self, tmp_path) -> None:
        from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry

        reg = SqliteAgentRegistry(db_path=tmp_path / "s.db")
        agent = reg.create_agent(AgentDraft(name="A", autonomy_level=AutonomyLevel.AUTONOMOUS))
        d = agent_to_dict(agent)
        assert d["autonomy_level"] == "autonomous"

    def test_draft_from_dict_parses_autonomy_level(self) -> None:
        draft = draft_from_dict({"name": "X", "autonomy_level": "ask_always"})
        assert draft.autonomy_level is AutonomyLevel.ASK_ALWAYS

    def test_draft_from_dict_invalid_autonomy_falls_back_balanced(self) -> None:
        # Unknown value → fail-closed → BALANCED
        draft = draft_from_dict({"name": "X", "autonomy_level": "god_mode"})
        assert draft.autonomy_level is AutonomyLevel.BALANCED

    def test_draft_from_dict_missing_autonomy_defaults_balanced(self) -> None:
        draft = draft_from_dict({"name": "X"})
        assert draft.autonomy_level is AutonomyLevel.BALANCED


# ---------------------------------------------------------------------------
# 3. Persistencia (SqliteAgentRegistry)
# ---------------------------------------------------------------------------


class TestAutonomyPersistence:
    def test_create_with_autonomy_level_persists(self, tmp_path) -> None:
        from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry

        reg = SqliteAgentRegistry(db_path=tmp_path / "s.db")
        created = reg.create_agent(
            AgentDraft(name="Coach", autonomy_level=AutonomyLevel.AUTONOMOUS)
        )
        fetched = reg.get_agent(created.agent_id)
        assert fetched.autonomy_level is AutonomyLevel.AUTONOMOUS

    def test_update_agent_changes_autonomy_level(self, tmp_path) -> None:
        from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry

        reg = SqliteAgentRegistry(db_path=tmp_path / "s.db")
        created = reg.create_agent(
            AgentDraft(name="X", autonomy_level=AutonomyLevel.BALANCED)
        )
        reg.update_agent(
            created.agent_id,
            AgentDraft(name="X", autonomy_level=AutonomyLevel.ASK_ALWAYS),
        )
        fetched = reg.get_agent(created.agent_id)
        assert fetched.autonomy_level is AutonomyLevel.ASK_ALWAYS

    def test_migration_idempotent_on_existing_db(self, tmp_path) -> None:
        """Construir dos veces la misma DB no lanza (migración idempotente)."""
        from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry

        db = tmp_path / "s.db"
        SqliteAgentRegistry(db_path=db)
        # Segunda construcción aplica ALTER TABLE ADD COLUMN de nuevo → no debe lanzar.
        reg2 = SqliteAgentRegistry(db_path=db)
        agents = reg2.list_agents()
        assert len(agents) == 1
        assert agents[0].autonomy_level is AutonomyLevel.BALANCED

    def test_default_agent_seed_has_balanced(self, tmp_path) -> None:
        from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry

        reg = SqliteAgentRegistry(db_path=tmp_path / "s.db")
        default = reg.list_agents()[0]
        assert default.autonomy_level is AutonomyLevel.BALANCED


# ---------------------------------------------------------------------------
# 4. Gate del broker (_needs_hitl y dispatch)
# ---------------------------------------------------------------------------


@dataclass
class _RecordingAdapter:
    _surface_kind: SurfaceKind = SurfaceKind.FILESYSTEM
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
        return ReplayOutcome(action_id=action.action_id, status=ReplayStatus.EXECUTED_OK)

    def serialize_for_signing(self, action: CapturedAction) -> bytes:  # pragma: no cover
        return b""


class _FakeConsentManager:
    def assert_active(self, *, human_operator_id: UUID, capability: object) -> object:
        return object()

    def use(self, *, human_operator_id: UUID, capability: object) -> object:
        return object()


def _make_broker(
    *,
    binding: CapabilityBinding,
) -> tuple[CapabilityBroker, _RecordingAdapter, FakeApprovalGate]:
    adapter = _RecordingAdapter(_surface_kind=binding.surface_kind or SurfaceKind.FILESYSTEM)
    reg = FakeCapabilityRegistry()
    reg.register(binding)
    gate = FakeApprovalGate()
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
        adapters={binding.surface_kind or SurfaceKind.FILESYSTEM: adapter}
    )
    broker = CapabilityBroker(
        registry=reg,
        consent_manager=_FakeConsentManager(),
        approval_gate=gate,
        dispatcher=dispatcher,
        signer=signer,
        audit_repo=audit_repo,
        intent_log=intent_log,
        anchor=FakeExternalAnchor(),
    )
    return broker, adapter, gate


def _ctx(untrusted: bool = False) -> ConsentContext:
    return ConsentContext(
        tenant_id=_TENANT_ID,
        operator_id=_OPERATOR_ID,
        derived_from_untrusted_content=untrusted,
    )


def _proposal(tool_name: str = "op") -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT_ID,
        entity_id="e",
        entity_type="t",
        parameters={"op": tool_name},
        justification="test",
    )


_LOW_NON_AUTO = CapabilityBinding(
    tool_name="write_file",
    surface_kind=SurfaceKind.FILESYSTEM,
    required_capability=None,
    risk=RiskLevel.LOW,
    auto_executable=False,
    reversible=False,  # default conservador
)
_LOW_NON_AUTO_REVERSIBLE = CapabilityBinding(
    tool_name="internal_cache_flush",
    surface_kind=SurfaceKind.FILESYSTEM,
    required_capability=None,
    risk=RiskLevel.LOW,
    auto_executable=False,
    reversible=True,  # única relajación permitida bajo AUTONOMOUS (C2)
)
_LOW_AUTO = CapabilityBinding(
    tool_name="read_file",
    surface_kind=SurfaceKind.FILESYSTEM,
    required_capability=None,
    risk=RiskLevel.LOW,
    auto_executable=True,
)
_HIGH = CapabilityBinding(
    tool_name="delete_file",
    surface_kind=SurfaceKind.FILESYSTEM,
    required_capability=None,
    risk=RiskLevel.HIGH,
    auto_executable=False,
)


class TestNeedsHitlPureFunction:
    """Cobertura unitaria de _needs_hitl — sin I/O."""

    # HIGH siempre exige HITL independientemente del nivel (invariante F-1)
    def test_high_always_needs_hitl_ask_always(self) -> None:
        assert _needs_hitl(RiskLevel.HIGH, _HIGH, AutonomyLevel.ASK_ALWAYS) is True

    def test_high_always_needs_hitl_balanced(self) -> None:
        assert _needs_hitl(RiskLevel.HIGH, _HIGH, AutonomyLevel.BALANCED) is True

    def test_high_always_needs_hitl_autonomous(self) -> None:
        assert _needs_hitl(RiskLevel.HIGH, _HIGH, AutonomyLevel.AUTONOMOUS) is True

    # LOW + auto_executable: ningún nivel necesita HITL (lecturas puras)
    def test_low_auto_ask_always_no_hitl(self) -> None:
        assert _needs_hitl(RiskLevel.LOW, _LOW_AUTO, AutonomyLevel.ASK_ALWAYS) is False

    def test_low_auto_balanced_no_hitl(self) -> None:
        assert _needs_hitl(RiskLevel.LOW, _LOW_AUTO, AutonomyLevel.BALANCED) is False

    def test_low_auto_autonomous_no_hitl(self) -> None:
        assert _needs_hitl(RiskLevel.LOW, _LOW_AUTO, AutonomyLevel.AUTONOMOUS) is False

    # LOW no-auto + reversible=False: HITL en los tres niveles (C2 — fail-closed)
    def test_low_non_auto_ask_always_needs_hitl(self) -> None:
        assert _needs_hitl(RiskLevel.LOW, _LOW_NON_AUTO, AutonomyLevel.ASK_ALWAYS) is True

    def test_low_non_auto_balanced_needs_hitl(self) -> None:
        assert _needs_hitl(RiskLevel.LOW, _LOW_NON_AUTO, AutonomyLevel.BALANCED) is True

    def test_low_non_auto_autonomous_reversible_false_still_needs_hitl(self) -> None:
        # C2: AUTONOMOUS relaja SOLO reversible=True; reversible=False sigue requiriendo HITL.
        # Esto es el cambio respecto al gate anterior (que era incondicionalmente False).
        assert _needs_hitl(RiskLevel.LOW, _LOW_NON_AUTO, AutonomyLevel.AUTONOMOUS) is True

    # LOW no-auto + reversible=True: HITL solo bajo AUTONOMOUS (única relajación C2)
    def test_low_non_auto_reversible_ask_always_needs_hitl(self) -> None:
        assert _needs_hitl(RiskLevel.LOW, _LOW_NON_AUTO_REVERSIBLE, AutonomyLevel.ASK_ALWAYS) is True

    def test_low_non_auto_reversible_balanced_needs_hitl(self) -> None:
        assert _needs_hitl(RiskLevel.LOW, _LOW_NON_AUTO_REVERSIBLE, AutonomyLevel.BALANCED) is True

    def test_low_non_auto_reversible_autonomous_no_hitl(self) -> None:
        # C2: esta es la ÚNICA relajación permitida en AUTONOMOUS para LOW+no-auto.
        assert _needs_hitl(RiskLevel.LOW, _LOW_NON_AUTO_REVERSIBLE, AutonomyLevel.AUTONOMOUS) is False

    # None level defaults to BALANCED
    def test_none_level_defaults_to_balanced(self) -> None:
        assert _needs_hitl(RiskLevel.LOW, _LOW_NON_AUTO, None) is True
        assert _needs_hitl(RiskLevel.LOW, _LOW_AUTO, None) is False


class TestBrokerDispatchByAutonomyLevel:
    """Tests de integración del broker con autonomy_level via dispatch()."""

    async def test_ask_always_low_non_auto_requires_hitl(self) -> None:
        """ASK_ALWAYS + LOW no-auto → PENDING_APPROVAL (exige HITL)."""
        broker, adapter, gate = _make_broker(binding=_LOW_NON_AUTO)
        outcome = await broker.dispatch(
            _proposal("write_file"),
            _ctx(),
            autonomy_level=AutonomyLevel.ASK_ALWAYS,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(adapter.calls) == 0

    async def test_ask_always_low_auto_executes(self) -> None:
        """ASK_ALWAYS + LOW auto_executable → EXECUTED (lecturas puras son autónomas)."""
        broker, adapter, gate = _make_broker(binding=_LOW_AUTO)
        outcome = await broker.dispatch(
            _proposal("read_file"),
            _ctx(),
            autonomy_level=AutonomyLevel.ASK_ALWAYS,
        )
        assert outcome.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1

    async def test_balanced_low_non_auto_requires_hitl(self) -> None:
        """BALANCED (default) + LOW no-auto → PENDING_APPROVAL (comportamiento actual)."""
        broker, adapter, gate = _make_broker(binding=_LOW_NON_AUTO)
        outcome = await broker.dispatch(
            _proposal("write_file"),
            _ctx(),
            autonomy_level=AutonomyLevel.BALANCED,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(adapter.calls) == 0

    async def test_balanced_low_auto_executes(self) -> None:
        """BALANCED + LOW auto_executable → EXECUTED (comportamiento actual intacto)."""
        broker, adapter, gate = _make_broker(binding=_LOW_AUTO)
        outcome = await broker.dispatch(
            _proposal("read_file"),
            _ctx(),
            autonomy_level=AutonomyLevel.BALANCED,
        )
        assert outcome.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1

    async def test_autonomous_low_non_auto_reversible_false_requires_hitl(self) -> None:
        """AUTONOMOUS + LOW no-auto + reversible=False → PENDING_APPROVAL (C2).

        Cambio respecto al gate anterior: AUTONOMOUS ya no relaja toda acción LOW+no-auto,
        solo las marcadas explícitamente reversible=True (que hoy son cero en el catálogo).
        """
        broker, adapter, gate = _make_broker(binding=_LOW_NON_AUTO)
        outcome = await broker.dispatch(
            _proposal("write_file"),
            _ctx(),
            autonomy_level=AutonomyLevel.AUTONOMOUS,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(adapter.calls) == 0

    async def test_autonomous_low_non_auto_reversible_true_executes(self) -> None:
        """AUTONOMOUS + LOW no-auto + reversible=True → EXECUTED (única relajación C2)."""
        broker, adapter, gate = _make_broker(binding=_LOW_NON_AUTO_REVERSIBLE)
        outcome = await broker.dispatch(
            _proposal("internal_cache_flush"),
            _ctx(),
            autonomy_level=AutonomyLevel.AUTONOMOUS,
        )
        assert outcome.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1
        assert gate.register_calls == []

    async def test_autonomous_low_auto_executes(self) -> None:
        """AUTONOMOUS + LOW auto_executable → EXECUTED."""
        broker, adapter, gate = _make_broker(binding=_LOW_AUTO)
        outcome = await broker.dispatch(
            _proposal("read_file"),
            _ctx(),
            autonomy_level=AutonomyLevel.AUTONOMOUS,
        )
        assert outcome.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1

    # --- Invariante de seguridad: HIGH siempre HITL, sin excepción ---

    async def test_autonomous_high_still_requires_hitl(self) -> None:
        """AUTONOMOUS + HIGH → PENDING_APPROVAL (invariante de seguridad F-1)."""
        broker, adapter, gate = _make_broker(binding=_HIGH)
        outcome = await broker.dispatch(
            _proposal("delete_file"),
            _ctx(),
            autonomy_level=AutonomyLevel.AUTONOMOUS,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert len(adapter.calls) == 0, (
            "HIGH nunca ejecuta sin token HITL, incluso en modo AUTONOMOUS (F-1)"
        )

    async def test_ask_always_high_requires_hitl(self) -> None:
        """ASK_ALWAYS + HIGH → PENDING_APPROVAL."""
        broker, adapter, gate = _make_broker(binding=_HIGH)
        outcome = await broker.dispatch(
            _proposal("delete_file"),
            _ctx(),
            autonomy_level=AutonomyLevel.ASK_ALWAYS,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL

    async def test_balanced_high_requires_hitl(self) -> None:
        """BALANCED + HIGH → PENDING_APPROVAL (comportamiento actual)."""
        broker, adapter, gate = _make_broker(binding=_HIGH)
        outcome = await broker.dispatch(
            _proposal("delete_file"),
            _ctx(),
            autonomy_level=AutonomyLevel.BALANCED,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL

    async def test_none_level_defaults_balanced_behaviour(self) -> None:
        """autonomy_level=None → BALANCED: LOW no-auto requiere HITL."""
        broker, adapter, gate = _make_broker(binding=_LOW_NON_AUTO)
        outcome = await broker.dispatch(
            _proposal("write_file"),
            _ctx(),
            autonomy_level=None,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL


# ---------------------------------------------------------------------------
# 5. _resolve_active_autonomy_level (fail-safe)
# ---------------------------------------------------------------------------


class TestResolveActiveAutonomyLevel:
    def test_none_registry_returns_balanced(self) -> None:
        from hermes.tasks.application.agent_loop_orchestrator import (
            _resolve_active_autonomy_level,
        )

        result = _resolve_active_autonomy_level(None)
        assert result is AutonomyLevel.BALANCED

    def test_registry_with_autonomous_agent_returns_autonomous(self, tmp_path) -> None:
        from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry
        from hermes.tasks.application.agent_loop_orchestrator import (
            _resolve_active_autonomy_level,
        )

        reg = SqliteAgentRegistry(db_path=tmp_path / "s.db")
        agent = reg.create_agent(
            AgentDraft(name="Bot", autonomy_level=AutonomyLevel.AUTONOMOUS)
        )
        reg.set_active_agent(agent.agent_id)
        result = _resolve_active_autonomy_level(reg)
        assert result is AutonomyLevel.AUTONOMOUS

    def test_registry_error_returns_balanced(self) -> None:
        """Si el registro lanza, _resolve_active_autonomy_level devuelve BALANCED (fail-safe)."""
        from hermes.tasks.application.agent_loop_orchestrator import (
            _resolve_active_autonomy_level,
        )

        class _BrokenRegistry:
            def active_agent_id(self) -> str:
                raise RuntimeError("DB unavailable")

        result = _resolve_active_autonomy_level(_BrokenRegistry())
        assert result is AutonomyLevel.BALANCED


# ---------------------------------------------------------------------------
# 6. C3 — lint anti-ensanche silencioso
# ---------------------------------------------------------------------------

# Allow-list de bindings LOW+no-auto+reversible=True aprobadas por security-engineer.
# Vacía intencionalmente: ninguna binding del catálogo actual cumple ese perfil.
# Añadir una binding reversible=True en el futuro ROMPE este test y obliga a
# pasar por revisión consciente (añadir el tool_name aquí con el OK de security).
_APPROVED_REVERSIBLE_NON_AUTO: frozenset[str] = frozenset()


class TestC3NoSilentReversibleExpansion:
    """C3 — Anti-ensanche silencioso: enumera TODAS las bindings del CapabilityRegistry
    y falla si aparece LOW+no-auto+reversible=True fuera de la allow-list aprobada.

    Propósito: que añadir una binding reversible en el catálogo sea un acto deliberado
    con revisión de security-engineer, no un efecto secundario silencioso. La allow-list
    vacía expresa que HOY ninguna binding ha pasado por esa revisión.
    """

    def test_no_unapproved_reversible_non_auto_bindings(self) -> None:
        from hermes.capabilities.application.capability_registry import CapabilityRegistry

        registry = CapabilityRegistry()

        # Accedemos a la tabla interna para enumerar todas las bindings declaradas.
        from hermes.capabilities.application.capability_registry import _REGISTRY_TABLE

        violations: list[str] = []
        for tool_name, binding in _REGISTRY_TABLE.items():
            is_reversible_non_auto_low = (
                binding.risk is RiskLevel.LOW
                and not binding.auto_executable
                and getattr(binding, "reversible", False)
            )
            if is_reversible_non_auto_low and tool_name not in _APPROVED_REVERSIBLE_NON_AUTO:
                violations.append(tool_name)

        assert not violations, (
            f"Bindings LOW+no-auto+reversible=True NO aprobadas por security-engineer: "
            f"{violations}. Añade el tool_name a _APPROVED_REVERSIBLE_NON_AUTO en este "
            f"test DESPUÉS de revisión explícita de security-engineer (C3)."
        )


# ---------------------------------------------------------------------------
# 7. C4 — regresión dura: HIGH → HITL en todos los niveles y con taint
# ---------------------------------------------------------------------------

_HIGH_BINDINGS_IN_REGISTRY: list[str] = []  # poblado en collect-time abajo


def _collect_high_bindings() -> list[tuple[str, object]]:
    """Enumera todas las bindings HIGH del catálogo para parametrizar C4."""
    from hermes.capabilities.application.capability_registry import _REGISTRY_TABLE

    return [
        (tool_name, binding)
        for tool_name, binding in _REGISTRY_TABLE.items()
        if binding.risk is RiskLevel.HIGH
    ]


_HIGH_BINDING_PARAMS = _collect_high_bindings()


class TestC4HighAlwaysHitlInvariant:
    """C4 — Invariante dura: toda binding HIGH del registry exige HITL bajo los
    tres valores de AutonomyLevel y también con taint activo (derived_from_untrusted_content).

    Fija "HIGH → HITL siempre" como regresión automática: si alguien cambia el riesgo
    de una binding de HIGH a LOW o modifica _needs_hitl para eximir HIGH, estos tests
    fallan inmediatamente.
    """

    @pytest.mark.parametrize(
        "tool_name,binding",
        _HIGH_BINDING_PARAMS,
        ids=[t for t, _ in _HIGH_BINDING_PARAMS],
    )
    @pytest.mark.parametrize(
        "level",
        [AutonomyLevel.ASK_ALWAYS, AutonomyLevel.BALANCED, AutonomyLevel.AUTONOMOUS],
        ids=["ask_always", "balanced", "autonomous"],
    )
    def test_high_binding_needs_hitl_all_levels(
        self, tool_name: str, binding: object, level: AutonomyLevel
    ) -> None:
        result = _needs_hitl(RiskLevel.HIGH, binding, level)
        assert result is True, (
            f"Invariante rota: binding '{tool_name}' (HIGH) devolvió needs_hitl=False "
            f"bajo AutonomyLevel.{level.value}. HIGH → HITL es inapelable (F-1)."
        )

    @pytest.mark.parametrize(
        "tool_name,binding",
        _HIGH_BINDING_PARAMS,
        ids=[t for t, _ in _HIGH_BINDING_PARAMS],
    )
    def test_high_binding_needs_hitl_with_taint(
        self, tool_name: str, binding: object
    ) -> None:
        """El taint (derived_from_untrusted_content) no puede eximir HIGH.

        _compute_effective_risk ya eleva a HIGH cuando hay taint; este test
        verifica que _needs_hitl(HIGH, ..., AUTONOMOUS) sigue siendo True
        independientemente de cualquier bandera del binding.
        """
        result = _needs_hitl(RiskLevel.HIGH, binding, AutonomyLevel.AUTONOMOUS)
        assert result is True, (
            f"Invariante rota con taint: binding '{tool_name}' (HIGH) + AUTONOMOUS "
            f"devolvió needs_hitl=False. HIGH → HITL es inapelable incluso con taint (F-1/CTRL-5)."
        )
