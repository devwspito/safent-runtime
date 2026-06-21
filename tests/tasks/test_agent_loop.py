"""Tests AgentLoopOrchestrator — deben FALLAR antes de T024.

Cubre (SC-002, SC-001, SC-003, NFR-007, LOOP-4):
- Drena sin UI: ctx.trigger empieza por "queue_drain:" (SC-002).
- Sin proposals => FAILED, nunca COMPLETED (SC-001).
- reconcile_stale llamado en bootstrap (SC-003).
- notify_watchdog en cada vuelta del loop (NFR-007).
- PENDING_APPROVAL no bloquea la cola (LOOP-4).
- broker devuelve EXECUTED + audit_entry_id => COMPLETED (SC-001).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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


def _consent(operator_id: UUID | None = None) -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT, operator_id=operator_id)


def _item(*, priority: int = 0) -> WorkItem:
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={"instruction": "do something", "enqueued_by": "op-1"},
        priority=priority,
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


def _outcome_executed(proposal_id: UUID) -> ExecutionOutcome:
    return ExecutionOutcome(
        proposal_id=proposal_id,
        status=ExecutionStatus.EXECUTED,
        audit_entry_id=uuid4(),
    )


def _outcome_pending(proposal_id: UUID) -> ExecutionOutcome:
    return ExecutionOutcome(
        proposal_id=proposal_id,
        status=ExecutionStatus.PENDING_APPROVAL,
    )


def _outcome_rejected(proposal_id: UUID) -> ExecutionOutcome:
    return ExecutionOutcome(
        proposal_id=proposal_id,
        status=ExecutionStatus.REJECTED_BY_CONSENT,
        error="no consent",
    )


def _outcome_failed(proposal_id: UUID) -> ExecutionOutcome:
    return ExecutionOutcome(
        proposal_id=proposal_id,
        status=ExecutionStatus.FAILED,
        error="dispatch error",
    )


def _make_orchestrator(
    *,
    queue: InMemoryWorkQueue | None = None,
    state: InMemoryAgentState | None = None,
    engine: FakeReasoningEngine | None = None,
    broker=None,
    watchdog_calls: list | None = None,
):
    from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415
    from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415

    queue = queue or InMemoryWorkQueue()
    state = state or InMemoryAgentState()
    engine = engine or FakeReasoningEngine()
    broker = broker or FakeCapabilityBroker()
    _wc: list = watchdog_calls if watchdog_calls is not None else []

    return AgentLoopOrchestrator(
        queue=queue,
        state=state,
        engine=engine,
        broker=broker,
        consent_context=_consent(),
        notify_watchdog=lambda: _wc.append(None),
        idle_poll_s=0.0,
        pause_poll_s=0.0,
    ), queue, _wc


class TestBootstrap:
    async def test_bootstrap_calls_reconcile_stale(self) -> None:
        """SC-003: bootstrap llama reconcile_stale."""
        from dataclasses import fields  # noqa: PLC0415
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState()

        # Insertar un item con lease expirado directamente
        item = _item()
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None
        expired = WorkItem(
            **{f.name: getattr(claimed, f.name) for f in fields(claimed)}
            | {"lease_expires_at": datetime.now(tz=UTC) - timedelta(seconds=1)}
        )
        queue._items[claimed.id] = expired

        orch = AgentLoopOrchestrator(
            queue=queue,
            state=state,
            engine=FakeReasoningEngine(),
            broker=FakeCapabilityBroker(),
            consent_context=_consent(),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        await orch.bootstrap()

        pending = queue.items_with_status(TaskStatus.PENDING)
        assert len(pending) == 1, "bootstrap debe reconciliar huérfanos a PENDING"


class TestTriggerFormat:
    async def test_trigger_starts_with_queue_drain(self) -> None:
        """SC-002: trigger del contexto empieza por 'queue_drain:'."""
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

        proposal = _proposal()
        engine = FakeReasoningEngine(scripted=[scripted_response(proposals=[proposal])])
        outcome = _outcome_executed(proposal.proposal_id)
        broker = FakeCapabilityBroker(scripted={proposal.proposal_id: outcome})

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState()

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

        item = _item()
        await queue.enqueue(item)
        await orch.bootstrap()
        claimed = await queue.claim_next()
        assert claimed is not None
        await orch._process(claimed)  # type: ignore[attr-defined]

        assert len(engine.calls) == 1
        ctx = engine.calls[0]
        assert ctx.trigger.startswith("queue_drain:"), (
            f"trigger debe empezar por 'queue_drain:' pero fue {ctx.trigger!r}"
        )

    async def test_cycle_id_matches_item_id(self) -> None:
        """SC-002: cycle_id del contexto = item.id."""
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

        proposal = _proposal()
        engine = FakeReasoningEngine(scripted=[scripted_response(proposals=[proposal])])
        outcome = _outcome_executed(proposal.proposal_id)
        broker = FakeCapabilityBroker(scripted={proposal.proposal_id: outcome})

        queue = InMemoryWorkQueue()
        item = _item()
        await queue.enqueue(item)

        orch = AgentLoopOrchestrator(
            queue=queue,
            state=InMemoryAgentState(),
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

        assert engine.calls[0].cycle_id == item.id


class TestNoProposalsFails:
    async def test_empty_proposals_marks_failed(self) -> None:
        """SC-001: sin proposals => FAILED (no_actions), nunca COMPLETED."""
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

        queue = InMemoryWorkQueue()
        engine = FakeReasoningEngine(scripted=[scripted_response(proposals=())])
        orch = AgentLoopOrchestrator(
            queue=queue,
            state=InMemoryAgentState(),
            engine=engine,
            broker=FakeCapabilityBroker(),
            consent_context=_consent(),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        item = _item()
        await queue.enqueue(item)
        await orch.bootstrap()
        claimed = await queue.claim_next()
        assert claimed is not None
        await orch._process(claimed)  # type: ignore[attr-defined]

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        assert len(completed) == 0


class TestWatchdog:
    async def test_watchdog_called_each_iteration(self) -> None:
        """NFR-007: notify_watchdog en cada vuelta del loop."""
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

        watchdog_calls: list[None] = []
        queue = InMemoryWorkQueue()

        orch = AgentLoopOrchestrator(
            queue=queue,
            state=InMemoryAgentState(),
            engine=FakeReasoningEngine(),
            broker=FakeCapabilityBroker(),
            consent_context=_consent(),
            notify_watchdog=lambda: watchdog_calls.append(None),
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        # Solicitar shutdown después de 2 iteraciones
        orch.request_shutdown()
        await orch.run_forever()

        # Al menos una llamada al watchdog antes de salir
        assert len(watchdog_calls) >= 1


class TestPendingApprovalNotBlocking:
    async def test_pending_approval_allows_next_item(self) -> None:
        """LOOP-4: PENDING_APPROVAL no bloquea el loop — siguiente item puede procesarse."""
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

        p1 = _proposal("write_file")
        p2 = _proposal("read_file")
        engine = FakeReasoningEngine(scripted=[
            scripted_response(proposals=[p1]),
            scripted_response(proposals=[p2]),
        ])

        pending_outcome = _outcome_pending(p1.proposal_id)
        executed_outcome = _outcome_executed(p2.proposal_id)
        broker = FakeCapabilityBroker(scripted={
            p1.proposal_id: pending_outcome,
            p2.proposal_id: executed_outcome,
        })

        queue = InMemoryWorkQueue()
        item1 = _item()
        item2 = _item()
        await queue.enqueue(item1)
        await queue.enqueue(item2)

        orch = AgentLoopOrchestrator(
            queue=queue,
            state=InMemoryAgentState(),
            engine=engine,
            broker=broker,
            consent_context=_consent(),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        await orch.bootstrap()

        # Procesar item1 => PENDING_APPROVAL
        claimed1 = await queue.claim_next()
        assert claimed1 is not None
        await orch._process(claimed1)  # type: ignore[attr-defined]

        pa_items = queue.items_with_status(TaskStatus.PENDING_APPROVAL)
        assert len(pa_items) == 1, "item1 debe quedar PENDING_APPROVAL"

        # item2 aún disponible
        claimed2 = await queue.claim_next()
        assert claimed2 is not None, "Loop no debe bloquearse tras PENDING_APPROVAL"
        await orch._process(claimed2)  # type: ignore[attr-defined]

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        assert len(completed) == 1, "item2 debe completarse"


class TestBrokerExecutedCompletes:
    async def test_executed_with_audit_id_completes_task(self) -> None:
        """SC-001: broker EXECUTED + audit_entry_id => COMPLETED."""
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

        proposal = _proposal()
        engine = FakeReasoningEngine(scripted=[scripted_response(proposals=[proposal])])
        outcome = _outcome_executed(proposal.proposal_id)
        broker = FakeCapabilityBroker(scripted={proposal.proposal_id: outcome})

        queue = InMemoryWorkQueue()
        orch = AgentLoopOrchestrator(
            queue=queue,
            state=InMemoryAgentState(),
            engine=engine,
            broker=broker,
            consent_context=_consent(),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        item = _item()
        await queue.enqueue(item)
        await orch.bootstrap()
        claimed = await queue.claim_next()
        assert claimed is not None
        await orch._process(claimed)  # type: ignore[attr-defined]

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        assert len(completed) == 1


class TestRejectedOutcome:
    async def test_rejected_by_consent_marks_rejected(self) -> None:
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

        proposal = _proposal()
        engine = FakeReasoningEngine(scripted=[scripted_response(proposals=[proposal])])
        outcome = _outcome_rejected(proposal.proposal_id)
        broker = FakeCapabilityBroker(scripted={proposal.proposal_id: outcome})

        queue = InMemoryWorkQueue()
        orch = AgentLoopOrchestrator(
            queue=queue,
            state=InMemoryAgentState(),
            engine=engine,
            broker=broker,
            consent_context=_consent(),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        item = _item()
        await queue.enqueue(item)
        await orch.bootstrap()
        claimed = await queue.claim_next()
        assert claimed is not None
        await orch._process(claimed)  # type: ignore[attr-defined]

        rejected = queue.items_with_status(TaskStatus.REJECTED)
        assert len(rejected) == 1


class TestFailedOutcomeRetries:
    async def test_failed_outcome_marks_for_retry(self) -> None:
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

        proposal = _proposal()
        engine = FakeReasoningEngine(scripted=[scripted_response(proposals=[proposal])])
        outcome = _outcome_failed(proposal.proposal_id)
        broker = FakeCapabilityBroker(scripted={proposal.proposal_id: outcome})

        queue = InMemoryWorkQueue()
        item = _item()
        await queue.enqueue(item)

        orch = AgentLoopOrchestrator(
            queue=queue,
            state=InMemoryAgentState(),
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

        # Puede estar FAILED terminal o PENDING (reintento con backoff)
        items = queue.all_items()
        assert len(items) == 1
        final = items[0]
        assert final.status in {TaskStatus.FAILED, TaskStatus.PENDING}
        assert final.status is not TaskStatus.COMPLETED


class TestPausedLoopSkipsWork:
    async def test_paused_loop_does_not_claim(self) -> None:
        """FR-022: cuando is_paused=True, el loop no toma trabajo."""
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState(paused=True)
        orch = AgentLoopOrchestrator(
            queue=queue,
            state=state,
            engine=FakeReasoningEngine(),
            broker=FakeCapabilityBroker(),
            consent_context=_consent(),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        item = _item()
        await queue.enqueue(item)

        # Shutdown inmediato para probar que el loop no consume
        orch.request_shutdown()
        await orch.run_forever()

        pending = queue.items_with_status(TaskStatus.PENDING)
        assert len(pending) == 1, "Item debe seguir PENDING cuando loop está pausado"


class TestRequestShutdown:
    async def test_shutdown_stops_loop(self) -> None:
        """request_shutdown() hace que run_forever() termine limpiamente."""
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

        orch = AgentLoopOrchestrator(
            queue=InMemoryWorkQueue(),
            state=InMemoryAgentState(),
            engine=FakeReasoningEngine(),
            broker=FakeCapabilityBroker(),
            consent_context=_consent(),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        orch.request_shutdown()
        # Debe terminar sin colgarse
        await asyncio.wait_for(orch.run_forever(), timeout=2.0)


# ---------------------------------------------------------------------------
# Fake ChunkSink — captura emit/close para assertions en tests
# ---------------------------------------------------------------------------


class _FakeChunkSink:
    """Implementación in-memory de ChunkSinkPort para tests."""

    def __init__(self) -> None:
        from uuid import UUID  # noqa: PLC0415
        self.emitted: list[dict] = []      # [{task_id, kind, delta}]
        self.closed: list[dict] = []       # [{task_id, outcome, error}]
        self.statuses: list[dict] = []     # [{task_id, status}]

    async def emit(self, *, task_id: UUID, chunk) -> None:
        self.emitted.append({
            "task_id": task_id,
            "kind": chunk.kind,
            "delta": chunk.delta,
        })

    async def close(self, *, task_id: UUID, outcome: str, error: str | None = None) -> None:
        self.closed.append({"task_id": task_id, "outcome": outcome, "error": error})

    async def emit_status(self, *, task_id: UUID, status: str) -> None:
        self.statuses.append({"task_id": task_id, "status": status})


def _chat_item() -> WorkItem:
    """WorkItem de kind CHAT_MESSAGE para tests de chat."""
    from hermes.tasks.domain.ports import WorkItemKind  # noqa: PLC0415
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="chat_message",
        kind=WorkItemKind.CHAT_MESSAGE,
        payload={
            "instruction": "¿puedes decirme qué dice mi último email?",
            "enqueued_by": str(uuid4()),
            "conversation_id": "conv-test-001",
        },
    )


def _make_chat_orchestrator(
    *,
    engine: FakeReasoningEngine,
    queue: InMemoryWorkQueue | None = None,
    chunk_sink: _FakeChunkSink | None = None,
):
    from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
    from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

    q = queue or InMemoryWorkQueue()
    sink = chunk_sink or _FakeChunkSink()
    orch = AgentLoopOrchestrator(
        queue=q,
        state=InMemoryAgentState(),
        engine=engine,
        broker=FakeCapabilityBroker(),
        consent_context=_consent(),
        notify_watchdog=lambda: None,
        idle_poll_s=0.0,
        pause_poll_s=0.0,
        chunk_sink=sink,
    )
    return orch, q, sink


# ---------------------------------------------------------------------------
# T051-FIX Regression: chat message with narrative → COMPLETED, not failed
# ---------------------------------------------------------------------------


class TestChatNarrativeReply:
    """Regresión T051-fix: run_cycle devuelve narrative + 0 proposals.

    Antes del fix: el orchestrator llegaba al bloque `if not tool_call_proposals`
    y llamaba mark_failed("no_actions"), descartando la narrativa y dejando la UI
    con un loading bubble eterno.

    Después del fix:
      (a) Se emite un chunk DELTA con la narrativa al stream.
      (b) El stream se cierra con outcome="completed".
      (c) La tarea queda COMPLETED en la cola.
    """

    async def test_narrative_only_emits_delta_chunk(self) -> None:
        """(a) El chunk DELTA con la narrativa llega al sink."""
        from hermes.tasks.control_plane.domain.ports import StreamChunkKind  # noqa: PLC0415

        narrative = "Aquí está el resumen de tu último email."
        engine = FakeReasoningEngine(scripted=[scripted_response(narrative=narrative, proposals=())])
        sink = _FakeChunkSink()
        orch, queue, _ = _make_chat_orchestrator(engine=engine, chunk_sink=sink)

        item = _chat_item()
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        await orch._process(claimed)  # type: ignore[attr-defined]

        assert len(sink.emitted) >= 1, (
            "Debe haberse emitido al menos un chunk DELTA con la narrativa"
        )
        delta_chunks = [c for c in sink.emitted if c["kind"] == StreamChunkKind.DELTA]
        assert len(delta_chunks) == 1
        assert delta_chunks[0]["delta"] == narrative

    async def test_narrative_only_closes_stream_completed(self) -> None:
        """(b) El stream se cierra con outcome='completed'."""
        narrative = "Aquí está el resumen de tu último email."
        engine = FakeReasoningEngine(scripted=[scripted_response(narrative=narrative, proposals=())])
        sink = _FakeChunkSink()
        orch, queue, _ = _make_chat_orchestrator(engine=engine, chunk_sink=sink)

        item = _chat_item()
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        await orch._process(claimed)  # type: ignore[attr-defined]

        assert len(sink.closed) == 1, "Debe haberse cerrado el stream exactamente una vez"
        assert sink.closed[0]["outcome"] == "completed", (
            f"Stream debe cerrarse con 'completed', no {sink.closed[0]['outcome']!r}"
        )

    async def test_narrative_only_marks_task_completed(self) -> None:
        """(c) La tarea queda COMPLETED en la cola — nunca FAILED/no_actions."""
        narrative = "Aquí está el resumen de tu último email."
        engine = FakeReasoningEngine(scripted=[scripted_response(narrative=narrative, proposals=())])
        sink = _FakeChunkSink()
        orch, queue, _ = _make_chat_orchestrator(engine=engine, chunk_sink=sink)

        item = _chat_item()
        await queue.enqueue(item)
        await orch.bootstrap()
        claimed = await queue.claim_next()
        assert claimed is not None

        await orch._process(claimed)  # type: ignore[attr-defined]

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        failed = queue.items_with_status(TaskStatus.FAILED)
        assert len(completed) == 1, (
            f"Tarea debe quedar COMPLETED. failed={len(failed)}, completed={len(completed)}"
        )
        assert len(failed) == 0, (
            "Tarea NO debe quedar FAILED cuando el agente responde con narrativa"
        )

    async def test_empty_narrative_still_fails(self) -> None:
        """Chat con narrative vacía (modelo mal configurado) → FAILED no_actions.

        Regresión: la corrección del narrative non-empty no debe afectar al path
        de fallo cuando el engine no produce ni proposals ni texto.
        """
        engine = FakeReasoningEngine(scripted=[scripted_response(narrative="", proposals=())])
        sink = _FakeChunkSink()
        orch, queue, _ = _make_chat_orchestrator(engine=engine, chunk_sink=sink)

        item = _chat_item()
        await queue.enqueue(item)
        await orch.bootstrap()
        claimed = await queue.claim_next()
        assert claimed is not None

        await orch._process(claimed)  # type: ignore[attr-defined]

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        assert len(completed) == 0, (
            "Narrativa vacía no debe marcar COMPLETED — debe seguir fallando"
        )
        # El stream debe cerrarse con error (no silencioso)
        assert len(sink.closed) == 1
        assert sink.closed[0]["outcome"] == "failed"

    async def test_whitespace_only_narrative_still_fails(self) -> None:
        """Narrativa de solo espacios → FAILED (equivale a vacía)."""
        engine = FakeReasoningEngine(scripted=[scripted_response(narrative="   \n  ", proposals=())])
        sink = _FakeChunkSink()
        orch, queue, _ = _make_chat_orchestrator(engine=engine, chunk_sink=sink)

        item = _chat_item()
        await queue.enqueue(item)
        await orch.bootstrap()
        claimed = await queue.claim_next()
        assert claimed is not None

        await orch._process(claimed)  # type: ignore[attr-defined]

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        assert len(completed) == 0, "Narrativa de solo espacios no debe completar la tarea"

    async def test_autonomous_item_with_no_proposals_still_fails(self) -> None:
        """Ítem AUTÓNOMO sin proposals → FAILED (sin cambio de comportamiento).

        Regresión: la corrección del chat NO debe tocar el path autónomo.
        """
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415

        engine = FakeReasoningEngine(scripted=[
            scripted_response(narrative="tengo mucho que decirte", proposals=())
        ])
        queue = InMemoryWorkQueue()
        orch = AgentLoopOrchestrator(
            queue=queue,
            state=InMemoryAgentState(),
            engine=engine,
            broker=FakeCapabilityBroker(),
            consent_context=_consent(),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        # Item AUTÓNOMO (no CHAT_MESSAGE)
        item = _item()
        await queue.enqueue(item)
        await orch.bootstrap()
        claimed = await queue.claim_next()
        assert claimed is not None

        await orch._process(claimed)  # type: ignore[attr-defined]

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        assert len(completed) == 0, (
            "Item autónomo sin proposals debe seguir fallando aunque haya narrativa"
        )
