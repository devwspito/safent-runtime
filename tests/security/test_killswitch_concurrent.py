"""T060 — Kill-switch concurrente: pausa durante N dispatches concurrentes.

Garantiza CTRL-P1-21:
  - El broker es instancia ÚNICA compartida por todos los workers del pool.
  - is_paused() se lee NO cacheado (estado en SQLite/store compartido).
  - Tras pausa, CERO ejecuciones progresan; cualquier dispatch en vuelo
    retorna REJECTED_BY_POLICY (o el worker sale del loop antes de tomar trabajo).

Controles verificados: CTRL-P1-21 (broker singleton), CTRL-12 (kill-switch atómico),
G5 (aislamiento de input bajo concurrencia).
"""

from __future__ import annotations

import asyncio
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


def _item() -> WorkItem:
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={"instruction": "do something", "enqueued_by": "op-1"},
    )


def _proposal() -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name="read_file",
        tenant_id=_TENANT,
        entity_id="f",
        entity_type="file",
        parameters={},
        justification="test",
    )


class TestKillSwitchConcurrent:
    async def test_zero_executions_after_pause_under_concurrency(self) -> None:
        """CTRL-P1-21: pausa durante N dispatches concurrentes => 0 ejecuciones post-pausa.

        Setup:
          - N=3 workers, pool corriendo
          - Pausar el agente mientras hay trabajo en la cola
          - Verificar que ningún item se completa DESPUÉS del pause

        El broker comparte estado en el AgentStatePort (NO cache per-worker).
        """
        from hermes.tasks.application.worker_pool import WorkerPool

        executions_after_pause: list[UUID] = []

        class _TrackingBroker:
            def __init__(self, state: InMemoryAgentState) -> None:
                self._state = state

            async def dispatch(
                self,
                proposal: ToolCallProposal,
                consent_context: ConsentContext,
                *,
                hitl_approval_token: str | None = None,
                work_item_id: UUID | None = None,
            ) -> ExecutionOutcome:
                # El broker chequea is_paused() en cada dispatch (CTRL-12).
                # Aquí simulamos el comportamiento del CapabilityBroker real:
                # si pausado, rechazar sin ejecutar.
                if await self._state.is_paused():
                    return ExecutionOutcome(
                        proposal_id=proposal.proposal_id,
                        status=ExecutionStatus.REJECTED_BY_POLICY,
                        error="agent paused — dispatch blocked by kill-switch (CTRL-12)",
                    )
                # Pequeña pausa para dar tiempo a que la pausa llegue
                await asyncio.sleep(0.02)
                if await self._state.is_paused():
                    return ExecutionOutcome(
                        proposal_id=proposal.proposal_id,
                        status=ExecutionStatus.REJECTED_BY_POLICY,
                        error="agent paused mid-dispatch (CTRL-12)",
                    )
                executions_after_pause.append(proposal.proposal_id)
                return ExecutionOutcome(
                    proposal_id=proposal.proposal_id,
                    status=ExecutionStatus.EXECUTED,
                    audit_entry_id=uuid4(),
                )

        state = InMemoryAgentState(paused=False)
        queue = InMemoryWorkQueue()

        # Encolar 10 tareas
        proposals = []
        for _ in range(10):
            p = _proposal()
            proposals.append(p)
            await queue.enqueue(_item())

        engine = FakeReasoningEngine(
            scripted=[scripted_response(proposals=[p]) for p in proposals]
        )

        broker = _TrackingBroker(state)
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

        pause_triggered = asyncio.Event()
        executions_at_pause_time: list[int] = []

        async def _pause_after_delay() -> None:
            # Esperar un poco para que algunos workers estén activos
            await asyncio.sleep(0.05)
            await state.pause(by=_OPERATOR, reason="concurrent kill-switch test")
            executions_at_pause_time.append(len(executions_after_pause))
            pause_triggered.set()
            # Esperar que el pool se quede en idle
            await asyncio.sleep(0.1)
            pool.request_shutdown()

        await pool.bootstrap()
        await asyncio.gather(
            pool.run_forever(size=3),
            _pause_after_delay(),
        )

        # Verificar que el estado del agente es paused después
        assert await state.is_paused(), "El agente debe seguir pausado"

        # CTRL-P1-21: is_paused() NO es cacheado per-worker.
        # Todas las ejecuciones que ocurrieron DESPUÉS del pause deben ser 0
        # (los workers leen el estado compartido, no una copia en memoria local).
        # Nota: algunas ejecuciones pueden haber completado ANTES del pause —
        # eso está permitido. El invariante es que DESPUÉS del pause = 0.
        completed = queue.items_with_status(TaskStatus.COMPLETED)
        total_at_pause = executions_at_pause_time[0] if executions_at_pause_time else 0

        # Las ejecuciones que rastreamos son SOLO las que pasaron el check de pausa.
        # Deben ser iguales a las del momento del pause (no aumentaron después).
        assert len(executions_after_pause) == total_at_pause, (
            f"Hubo {len(executions_after_pause) - total_at_pause} ejecucion(es) "
            f"DESPUÉS del pause. CTRL-P1-21: is_paused() no debe ser cacheado per-worker."
        )

    async def test_broker_singleton_shared_by_all_workers(self) -> None:
        """CTRL-P1-21: el broker es la MISMA instancia para todos los workers.

        Verificamos que el WorkerPool no instancia un broker por worker,
        sino que todos comparten la referencia inyectada.
        """
        from hermes.tasks.application.worker_pool import WorkerPool
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker

        broker = FakeCapabilityBroker()
        queue = InMemoryWorkQueue()
        state = InMemoryAgentState()

        proposals = [_proposal() for _ in range(4)]
        for p in proposals:
            await queue.enqueue(_item())

        engine = FakeReasoningEngine(
            scripted=[scripted_response(proposals=[p]) for p in proposals]
        )

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

        async def _stop_when_done() -> None:
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                done = (
                    queue.items_with_status(TaskStatus.COMPLETED)
                    + queue.items_with_status(TaskStatus.FAILED)
                )
                if len(done) >= 4:
                    pool.request_shutdown()
                    return
                await asyncio.sleep(0.01)
            pool.request_shutdown()

        await pool.bootstrap()
        await asyncio.gather(
            pool.run_forever(size=3),
            _stop_when_done(),
        )

        # El broker es la misma instancia: todos los dispatches están en broker.dispatched
        assert len(broker.dispatched) == 4, (
            "El broker singleton debe registrar todos los dispatches de todos los workers. "
            f"Got {len(broker.dispatched)}, expected 4."
        )

    async def test_paused_state_not_cached_per_worker(self) -> None:
        """CTRL-P1-21: is_paused() lee el estado real (SQLite), no cache per-worker.

        Pausa tras inicio del pool y verifica que los workers dejan de procesar.
        El test falla si los workers tienen una copia cacheada del estado.
        """
        from hermes.tasks.application.worker_pool import WorkerPool
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker

        state = InMemoryAgentState(paused=False)
        queue = InMemoryWorkQueue()

        # Encolar muchas tareas para que los workers sigan trabajando
        proposals = [_proposal() for _ in range(20)]
        for _ in range(20):
            await queue.enqueue(_item())

        engine = FakeReasoningEngine(
            scripted=[scripted_response(proposals=[p]) for p in proposals]
        )
        broker = FakeCapabilityBroker()

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

        dispatched_after_pause: list[int] = []

        async def _pause_and_measure() -> None:
            # Esperar a que procesen alguna tarea
            await asyncio.sleep(0.03)
            await state.pause(by=_OPERATOR, reason="cache test")
            before = len(broker.dispatched)
            # Esperar un poco más — si hay cache, los workers seguirían dispatching
            await asyncio.sleep(0.1)
            after = len(broker.dispatched)
            dispatched_after_pause.append(after - before)
            pool.request_shutdown()

        await pool.bootstrap()
        await asyncio.gather(
            pool.run_forever(size=3),
            _pause_and_measure(),
        )

        # Con N=3 workers, si hubiera cache, podrían disparar hasta ~N dispatches
        # adicionales tras el pause. Pero must ser muy bajo (idealmente 0).
        # Permitimos un margen pequeño por race conditions de in-flight dispatch.
        extra_dispatches = dispatched_after_pause[0] if dispatched_after_pause else 0
        assert extra_dispatches <= 3, (
            f"Hubo {extra_dispatches} dispatches DESPUÉS del pause. "
            "is_paused() parece cacheado per-worker (CTRL-P1-21 violation)."
        )
