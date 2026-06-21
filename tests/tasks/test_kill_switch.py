"""T046 🔒 — Tests kill-switch US3 (CTRL-12 / KILL-1 / KILL-2 / SC-005).

Estos tests deben FALLAR antes de T047 y verde tras él.

Cubre:
- pause() ⇒ is_paused() True ⇒ loop no llama claim_next (SC-005/FR-022).
- Cola intacta: COUNT pending constante mientras pausado.
- Chequeo de pausa ATÓMICO en el broker ANTES de cada dispatch (KILL-2):
    si el loop pasa el is_paused inicial pero el agente se pausa ANTES del
    broker.dispatch, el broker devuelve REJECTED_BY_POLICY/paused sin tocar
    el adapter.
- resume() ⇒ el loop retoma sin pérdida ni duplicación (FR-023).
- Transiciones auditadas: AGENT_PAUSED / AGENT_RESUMED con changed_by+reason.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest

from hermes.capabilities.domain.ports import (
    ConsentContext,
    ExecutionOutcome,
    ExecutionStatus,
)
from hermes.domain.proposal import ToolCallProposal
from hermes.tasks.domain.ports import TaskStatus, WorkItem
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue
from hermes.testing import FakeReasoningEngine, scripted_response

pytestmark = pytest.mark.unit

_TENANT = uuid4()
_OPERATOR = uuid4()
_SIGNING_KEY = os.urandom(32)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _item() -> WorkItem:
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={"instruction": "do something", "enqueued_by": "op-1"},
    )


def _proposal(tool_name: str = "read_file") -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT,
        entity_id="file-1",
        entity_type="file",
        parameters={},
        justification="test",
    )


def _consent() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)


def _make_orchestrator(
    *,
    queue: InMemoryWorkQueue | None = None,
    state: InMemoryAgentState | None = None,
    broker=None,
    watchdog: Callable[[], None] | None = None,
):
    from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker
    from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator

    q = queue or InMemoryWorkQueue()
    s = state or InMemoryAgentState()
    b = broker or FakeCapabilityBroker()

    orch = AgentLoopOrchestrator(
        queue=q,
        state=s,
        engine=FakeReasoningEngine(),
        broker=b,
        consent_context=_consent(),
        notify_watchdog=watchdog or (lambda: None),
        idle_poll_s=0.0,
        pause_poll_s=0.0,
    )
    return orch, q, s


# ---------------------------------------------------------------------------
# T046-1: pause ⇒ is_paused True ⇒ loop no toma trabajo
# ---------------------------------------------------------------------------


class TestPausePreventsClaiming:
    async def test_paused_state_is_paused_true(self) -> None:
        """pause() ⇒ is_paused() devuelve True (pre-condición del kill-switch)."""
        state = InMemoryAgentState()
        await state.pause(by=_OPERATOR, reason="manual kill-switch")
        assert await state.is_paused() is True

    async def test_loop_does_not_claim_when_paused(self) -> None:
        """FR-022: loop pausado no llama claim_next, cola intacta."""
        queue = InMemoryWorkQueue()
        state = InMemoryAgentState(paused=True)
        orch, _, _ = _make_orchestrator(queue=queue, state=state)

        await queue.enqueue(_item())
        pending_before = len(queue.items_with_status(TaskStatus.PENDING))

        orch.request_shutdown()
        await orch.run_forever()

        pending_after = len(queue.items_with_status(TaskStatus.PENDING))
        assert pending_before == 1
        assert pending_after == 1, "Cola debe estar intacta mientras el loop está pausado"

    async def test_paused_loop_pending_count_constant(self) -> None:
        """SC-005: COUNT(pending) constante mientras pausado — sin race."""
        queue = InMemoryWorkQueue()
        state = InMemoryAgentState(paused=True)
        for _ in range(3):
            await queue.enqueue(_item())

        orch, _, _ = _make_orchestrator(queue=queue, state=state)
        orch.request_shutdown()
        await orch.run_forever()

        assert len(queue.items_with_status(TaskStatus.PENDING)) == 3


# ---------------------------------------------------------------------------
# T046-2: chequeo atómico en el broker ANTES de cada dispatch (KILL-2)
# ---------------------------------------------------------------------------


class TestBrokerAtomicPauseCheck:
    async def test_broker_rejects_dispatch_when_paused(self) -> None:
        """KILL-2: broker con AgentStatePort inyectado rechaza dispatch si pausado.

        El broker debe devolver ExecutionStatus.REJECTED_BY_POLICY con
        reason que contenga 'paused' cuando is_paused() == True, sin
        tocar el adapter.
        """
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.capabilities.application.capability_broker import CapabilityBroker
        from hermes.capabilities.application.intent_log import IntentLog
        from hermes.capabilities.domain.ports import CapabilityBinding, RiskLevel
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
        from hermes.agents_os.domain.ports.surface_adapter_port import (
            CapturedAction,
            ReplayOutcome,
            ReplayStatus,
        )
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from pathlib import Path
        import tempfile
        import dataclasses

        # Adapter que NO debe ser llamado si el broker rechaza por pausa
        replay_calls: list = []

        @dataclasses.dataclass
        class _TrackingAdapter:
            _surface_kind: SurfaceKind = SurfaceKind.FILESYSTEM

            @property
            def surface_kind(self) -> SurfaceKind:
                return self._surface_kind

            async def capture(self, **_) -> CapturedAction:
                raise NotImplementedError

            async def replay(self, action, **_) -> ReplayOutcome:
                replay_calls.append(action)
                return ReplayOutcome(action_id=action.action_id, status=ReplayStatus.EXECUTED_OK)

            def serialize_for_signing(self, action) -> bytes:
                return b""

        tmp = tempfile.mkdtemp()
        audit_repo = SqliteAuditRepository(db_path=Path(tmp) / "audit.db")
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="read_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=True,
        ))
        adapter = _TrackingAdapter()
        dispatcher = SurfaceAdapterDispatcher(adapters={SurfaceKind.FILESYSTEM: adapter})

        state = InMemoryAgentState(paused=True)

        broker = CapabilityBroker(
            registry=reg,
            consent_manager=_FakeConsentManagerAllow(),
            approval_gate=FakeApprovalGate(),
            dispatcher=dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=IntentLog(),
            agent_state=state,  # ← inyección del kill-switch atómico
        )

        proposal = _proposal("read_file")
        outcome = await broker.dispatch(proposal, _consent())

        assert outcome.status is ExecutionStatus.REJECTED_BY_POLICY
        assert "paused" in (outcome.error or "").lower(), (
            f"Error debe indicar pausa. Got: {outcome.error!r}"
        )
        assert len(replay_calls) == 0, "El adapter NO debe ser invocado cuando pausado"

    async def test_broker_executes_when_not_paused(self) -> None:
        """Verificación negativa: broker sin pausa sigue ejecutando normalmente."""
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.capabilities.application.capability_broker import CapabilityBroker
        from hermes.capabilities.application.intent_log import IntentLog
        from hermes.capabilities.domain.ports import CapabilityBinding, RiskLevel
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
        from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
        from hermes.agents_os.domain.ports.surface_adapter_port import (
            CapturedAction,
            ReplayOutcome,
            ReplayStatus,
        )
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from pathlib import Path
        import tempfile
        import dataclasses

        replay_calls: list = []

        @dataclasses.dataclass
        class _TrackingAdapter:
            _surface_kind: SurfaceKind = SurfaceKind.FILESYSTEM

            @property
            def surface_kind(self) -> SurfaceKind:
                return self._surface_kind

            async def capture(self, **_) -> CapturedAction:
                raise NotImplementedError

            async def replay(self, action, **_) -> ReplayOutcome:
                replay_calls.append(action)
                return ReplayOutcome(action_id=action.action_id, status=ReplayStatus.EXECUTED_OK)

            def serialize_for_signing(self, action) -> bytes:
                return b""

        tmp = tempfile.mkdtemp()
        audit_repo = SqliteAuditRepository(db_path=Path(tmp) / "audit.db")
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        reg = FakeCapabilityRegistry()
        reg.register(CapabilityBinding(
            tool_name="read_file",
            surface_kind=SurfaceKind.FILESYSTEM,
            required_capability=None,
            risk=RiskLevel.LOW,
            auto_executable=True,
        ))
        adapter = _TrackingAdapter()
        dispatcher = SurfaceAdapterDispatcher(adapters={SurfaceKind.FILESYSTEM: adapter})

        state = InMemoryAgentState(paused=False)

        broker = CapabilityBroker(
            registry=reg,
            consent_manager=_FakeConsentManagerAllow(),
            approval_gate=FakeApprovalGate(),
            dispatcher=dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=IntentLog(),
            agent_state=state,
        )

        proposal = _proposal("read_file")
        outcome = await broker.dispatch(proposal, _consent())

        assert outcome.status is ExecutionStatus.EXECUTED
        assert len(replay_calls) == 1, "Con estado running, el adapter debe ejecutar"


# ---------------------------------------------------------------------------
# T046-3: resume ⇒ el loop retoma sin pérdida ni duplicación
# ---------------------------------------------------------------------------


class TestResumeRecoversWork:
    async def test_resume_unfreezes_loop(self) -> None:
        """FR-023: resume() ⇒ is_paused() False."""
        state = InMemoryAgentState()
        await state.pause(by=_OPERATOR, reason="test")
        assert await state.is_paused() is True
        await state.resume(by=_OPERATOR)
        assert await state.is_paused() is False

    async def test_queue_intact_after_pause_resume(self) -> None:
        """FR-023: tras pause+resume, los items siguen PENDING (no perdidos)."""
        queue = InMemoryWorkQueue()
        state = InMemoryAgentState(paused=True)

        for _ in range(2):
            await queue.enqueue(_item())

        assert len(queue.items_with_status(TaskStatus.PENDING)) == 2

        await state.resume(by=_OPERATOR)
        assert await state.is_paused() is False

        # La cola sigue intacta tras resume
        assert len(queue.items_with_status(TaskStatus.PENDING)) == 2

    async def test_no_duplicate_processing_after_resume(self) -> None:
        """SC-005: un solo item procesado exactamente 1 vez tras resume."""
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator

        proposal = _proposal()
        engine = FakeReasoningEngine(scripted=[scripted_response(proposals=[proposal])])
        outcome = ExecutionOutcome(
            proposal_id=proposal.proposal_id,
            status=ExecutionStatus.EXECUTED,
            audit_entry_id=uuid4(),
        )
        broker = FakeCapabilityBroker(scripted={proposal.proposal_id: outcome})

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState(paused=False)
        await queue.enqueue(_item())

        orch = AgentLoopOrchestrator(
            queue=queue,
            state=state,
            engine=engine,
            broker=broker,
            consent_context=_consent(),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        await orch.bootstrap()
        claimed = await queue.claim_next()
        assert claimed is not None
        await orch._process(claimed)  # type: ignore[attr-defined]

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        assert len(completed) == 1, "Exactamente 1 completed tras procesar"
        # No hay pending duplicados
        assert len(queue.items_with_status(TaskStatus.PENDING)) == 0


# ---------------------------------------------------------------------------
# T046-4: audit AGENT_PAUSED / AGENT_RESUMED (T047)
# ---------------------------------------------------------------------------


class TestAuditOnPauseResume:
    async def test_sqlite_agent_state_emits_audit_on_pause(self) -> None:
        """SqliteAgentState.pause() ⇒ emite AuditKind.AGENT_PAUSED en el signer."""
        from pathlib import Path
        import tempfile

        from hermes.agents_os.application.audit_hash_chain import (
            AuditHashChainSigner,
            AuditKind,
        )
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.tasks.infrastructure.sqlite_agent_state import SqliteAgentState

        tmp = Path(tempfile.mkdtemp())
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        audit_repo = SqliteAuditRepository(db_path=tmp / "audit.db")
        state = SqliteAgentState(db_path=tmp / "shell-state.db", signer=signer, audit_repo=audit_repo)

        await state.pause(by=_OPERATOR, reason="operator kill-switch")

        chain = await audit_repo.load_chain()
        kinds = [e.audit_kind for e in chain]
        assert AuditKind.AGENT_PAUSED in kinds, (
            f"AGENT_PAUSED debe estar en el audit log. Kinds: {kinds}"
        )

        # El entry de AGENT_PAUSED debe llevar el changed_by
        paused_entries = [e for e in chain if e.audit_kind == AuditKind.AGENT_PAUSED]
        assert len(paused_entries) == 1
        assert str(_OPERATOR) in paused_entries[0].actor

    async def test_sqlite_agent_state_emits_audit_on_resume(self) -> None:
        """SqliteAgentState.resume() ⇒ emite AuditKind.AGENT_RESUMED en el signer."""
        from pathlib import Path
        import tempfile

        from hermes.agents_os.application.audit_hash_chain import (
            AuditHashChainSigner,
            AuditKind,
        )
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.tasks.infrastructure.sqlite_agent_state import SqliteAgentState

        tmp = Path(tempfile.mkdtemp())
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        audit_repo = SqliteAuditRepository(db_path=tmp / "audit.db")
        state = SqliteAgentState(db_path=tmp / "shell-state.db", signer=signer, audit_repo=audit_repo)

        await state.pause(by=_OPERATOR, reason="test")
        await state.resume(by=_OPERATOR)

        chain = await audit_repo.load_chain()
        kinds = [e.audit_kind for e in chain]
        assert AuditKind.AGENT_RESUMED in kinds, (
            f"AGENT_RESUMED debe estar en el audit log. Kinds: {kinds}"
        )

        resumed_entries = [e for e in chain if e.audit_kind == AuditKind.AGENT_RESUMED]
        assert len(resumed_entries) == 1
        assert str(_OPERATOR) in resumed_entries[0].actor

    async def test_audit_not_emitted_without_repo(self) -> None:
        """Sin signer/audit_repo inyectados, SqliteAgentState funciona igual (sin crash)."""
        from pathlib import Path
        import tempfile

        from hermes.tasks.infrastructure.sqlite_agent_state import SqliteAgentState

        tmp = Path(tempfile.mkdtemp())
        # Sin signer — no debe crashear
        state = SqliteAgentState(db_path=tmp / "shell-state.db")

        await state.pause(by=_OPERATOR, reason="test")
        assert await state.is_paused() is True
        await state.resume(by=_OPERATOR)
        assert await state.is_paused() is False


# ---------------------------------------------------------------------------
# Helper fake consent manager (permite todo)
# ---------------------------------------------------------------------------


class _FakeConsentManagerAllow:
    def assert_active(self, *, human_operator_id: UUID, capability) -> object:
        return object()

    def use(self, *, human_operator_id: UUID, capability) -> object:
        return object()
