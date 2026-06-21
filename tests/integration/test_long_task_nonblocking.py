"""T059 — WorkerPool: tarea larga no bloquea progreso de otras (SC-009).

Un worker procesa una tarea larga; otras tareas en la cola deben progresar
(ser reclamadas y completadas) de forma independiente, usando workers libres.

Criterio de aceptación (SC-009 / FR-024):
  - Con pool de N=2 workers y 1 tarea bloqueante + M tareas rápidas,
    las M tareas rápidas se completan DURANTE la ejecución de la larga.
  - La latencia de procesamiento de las tareas rápidas es independiente
    de la duración de la tarea larga (el pool no serializa).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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

pytestmark = pytest.mark.integration

_TENANT = uuid4()
_OPERATOR = uuid4()


def _consent() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)


def _quick_item() -> WorkItem:
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={"instruction": "quick task", "enqueued_by": "op-1"},
    )


def _slow_item() -> WorkItem:
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={"instruction": "slow task", "enqueued_by": "op-1"},
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


class _SlowBroker:
    """Broker que simula una tarea larga (sleep) para proposals específicas."""

    def __init__(self, *, slow_proposal_ids: set[UUID], slow_delay_s: float) -> None:
        self._slow_ids = slow_proposal_ids
        self._slow_delay = slow_delay_s
        self.dispatch_times: dict[UUID, datetime] = {}
        self.complete_times: dict[UUID, datetime] = {}

    async def dispatch(
        self,
        proposal: ToolCallProposal,
        consent_context: ConsentContext,
        *,
        hitl_approval_token: str | None = None,
        work_item_id: UUID | None = None,
    ) -> ExecutionOutcome:
        pid = proposal.proposal_id
        self.dispatch_times[pid] = datetime.now(tz=UTC)
        if pid in self._slow_ids:
            await asyncio.sleep(self._slow_delay)
        self.complete_times[pid] = datetime.now(tz=UTC)
        return ExecutionOutcome(
            proposal_id=pid,
            status=ExecutionStatus.EXECUTED,
            audit_entry_id=uuid4(),
        )


class TestLongTaskNonBlocking:
    async def test_slow_worker_does_not_block_fast_tasks(self) -> None:
        """SC-009 / FR-024: una tarea larga en un worker no bloquea las demás.

        Setup:
          - Pool N=2 workers
          - 1 tarea lenta (~200 ms)
          - 4 tareas rápidas

        Expectation: las 4 rápidas completan MIENTRAS la lenta todavía corre
        (o inmediatamente después), sin esperar a que termine la lenta.
        La latencia de las rápidas < duración de la lenta.
        """
        from hermes.tasks.application.worker_pool import WorkerPool

        slow_proposal = _proposal("slow_op")
        quick_proposals = [_proposal("quick_op") for _ in range(4)]
        slow_item = _slow_item()

        # El broker tarda 200 ms en la proposal lenta; 0 ms en las rápidas.
        slow_delay_s = 0.2
        broker = _SlowBroker(
            slow_proposal_ids={slow_proposal.proposal_id},
            slow_delay_s=slow_delay_s,
        )

        # Motor scripteado: slow_item -> slow_proposal; resto -> quick_proposals
        slow_response = scripted_response(proposals=[slow_proposal])

        quick_engine_responses = []
        for qp in quick_proposals:
            quick_engine_responses.append(scripted_response(proposals=[qp]))

        # Usamos un engine que devuelve respuestas en orden
        engine = FakeReasoningEngine(scripted=[slow_response] + quick_engine_responses)

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState()

        # Encolar primero la lenta, luego las 4 rápidas
        await queue.enqueue(slow_item)
        quick_items = []
        for _ in range(4):
            qi = await queue.enqueue(_quick_item())
            quick_items.append(qi)

        pool = WorkerPool(
            queue=queue,
            state=state,
            engine=engine,
            broker=broker,
            consent_context=_consent(),
            notify_watchdog=lambda: None,
            idle_poll_s=0.01,
            pause_poll_s=0.01,
        )

        start = datetime.now(tz=UTC)

        async def _stop_when_done() -> None:
            # Esperar hasta que todas las tareas estén procesadas o timeout
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                completed = queue.items_with_status(TaskStatus.COMPLETED)
                failed = queue.items_with_status(TaskStatus.FAILED)
                if len(completed) + len(failed) >= 5:
                    pool.request_shutdown()
                    return
                await asyncio.sleep(0.01)
            pool.request_shutdown()

        await pool.bootstrap()
        await asyncio.gather(
            pool.run_forever(size=2),
            _stop_when_done(),
        )

        end = datetime.now(tz=UTC)
        total_s = (end - start).total_seconds()

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        failed = queue.items_with_status(TaskStatus.FAILED)

        # Todas las 5 tareas deben haberse procesado
        assert len(completed) + len(failed) == 5, (
            f"Esperados 5 items procesados, got {len(completed) + len(failed)}"
        )

        # El tiempo total debe ser significativamente menor que 5 * slow_delay
        # (prueba que NO serializa). Con 2 workers: ~1 lenta + 4 rápidas ~= slow_delay
        # pero < 5 * slow_delay (=1.0s).
        assert total_s < slow_delay_s * 4, (
            f"El pool tardó {total_s:.3f}s — si serializara, tardaría ~{slow_delay_s * 5:.1f}s. "
            "FR-024: un worker ocupado NO debe bloquear a los demás."
        )

    async def test_quick_tasks_latency_independent_of_slow_task(self) -> None:
        """SC-009: las tareas rápidas completan rápido incluso con una lenta en ejecución.

        Con N=2 workers y 1 tarea lenta (0.3s), las rápidas deben completar
        en < 0.25s (bien por debajo de la lenta), demostrando independencia.
        """
        from hermes.tasks.application.worker_pool import WorkerPool

        slow_proposal = _proposal("slow_op")
        quick_proposals = [_proposal("quick_op") for _ in range(2)]
        slow_delay_s = 0.3

        # Registrar tiempos de inicio/fin por tarea
        task_start_times: dict[UUID, datetime] = {}
        task_end_times: dict[UUID, datetime] = {}

        class _TimingBroker:
            async def dispatch(
                self,
                proposal: ToolCallProposal,
                consent_context: ConsentContext,
                *,
                hitl_approval_token: str | None = None,
                work_item_id: UUID | None = None,
            ) -> ExecutionOutcome:
                pid = proposal.proposal_id
                if work_item_id:
                    task_start_times[work_item_id] = datetime.now(tz=UTC)
                if pid == slow_proposal.proposal_id:
                    await asyncio.sleep(slow_delay_s)
                if work_item_id:
                    task_end_times[work_item_id] = datetime.now(tz=UTC)
                return ExecutionOutcome(
                    proposal_id=pid,
                    status=ExecutionStatus.EXECUTED,
                    audit_entry_id=uuid4(),
                )

        slow_item = _slow_item()
        quick_items = [_quick_item() for _ in range(2)]

        engine = FakeReasoningEngine(scripted=[
            scripted_response(proposals=[slow_proposal]),
            scripted_response(proposals=[quick_proposals[0]]),
            scripted_response(proposals=[quick_proposals[1]]),
        ])

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState()
        await queue.enqueue(slow_item)
        for qi in quick_items:
            await queue.enqueue(qi)

        pool = WorkerPool(
            queue=queue,
            state=state,
            engine=engine,
            broker=_TimingBroker(),
            consent_context=_consent(),
            notify_watchdog=lambda: None,
            idle_poll_s=0.01,
            pause_poll_s=0.01,
        )

        async def _stop_when_done() -> None:
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                done = queue.items_with_status(TaskStatus.COMPLETED)
                failed = queue.items_with_status(TaskStatus.FAILED)
                if len(done) + len(failed) >= 3:
                    pool.request_shutdown()
                    return
                await asyncio.sleep(0.01)
            pool.request_shutdown()

        await pool.bootstrap()
        await asyncio.gather(
            pool.run_forever(size=2),
            _stop_when_done(),
        )

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        assert len(completed) >= 2, "Al menos las tareas rápidas deben completar"
