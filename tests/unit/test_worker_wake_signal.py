"""T037 — unit tests para WorkerWakeSignal (MonoWorkerWakeSignal).

Cubre:
- wake_one() interrumpe wait_for_work() inmediato (SC-006).
- Primer "trabajo" servido < 300 ms con fake enqueue.
- Timeout retorna False cuando no hay wake.
- Múltiples wake_one() antes de wait_for_work() son idempotentes (un drain).
- wake_all() equivale a wake_one() en implementación mono.
- wait_for_work(timeout=0) no bloquea (caso especial tests).
- Loop ocioso modificado: _idle() con MonoWorkerWakeSignal interrumpible.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from hermes.tasks.application.worker_wake_signal import MonoWorkerWakeSignal

pytestmark = pytest.mark.unit


class TestWakeOne:
    async def test_wake_one_interrupts_wait_immediately(self) -> None:
        """SC-006: wake_one() despierta wait_for_work() antes del timeout."""
        signal = MonoWorkerWakeSignal()

        async def _wake_after_delay() -> None:
            await asyncio.sleep(0.01)
            signal.wake_one()

        asyncio.create_task(_wake_after_delay())
        start = time.monotonic()
        got_wake = await signal.wait_for_work(timeout=5.0)
        elapsed = time.monotonic() - start

        assert got_wake is True
        assert elapsed < 0.3, f"wake tardó {elapsed:.3f}s, debería ser < 300ms"

    async def test_wait_for_work_returns_false_on_timeout(self) -> None:
        """Sin wake → retorna False tras el timeout."""
        signal = MonoWorkerWakeSignal()
        got_wake = await signal.wait_for_work(timeout=0.05)
        assert got_wake is False

    async def test_multiple_wakes_idempotent(self) -> None:
        """Varios wake_one() antes de wait_for_work() = un solo drain."""
        signal = MonoWorkerWakeSignal()
        signal.wake_one()
        signal.wake_one()
        signal.wake_one()

        result1 = await signal.wait_for_work(timeout=0.1)
        # Después del primer drain, el event queda cleared
        result2 = await signal.wait_for_work(timeout=0.05)

        assert result1 is True
        assert result2 is False

    async def test_wake_resets_after_wait(self) -> None:
        """Tras wait_for_work(), el signal se resetea y puede reutilizarse."""
        signal = MonoWorkerWakeSignal()
        signal.wake_one()
        await signal.wait_for_work(timeout=0.1)

        # Sin segundo wake, el siguiente wait debe expirar
        result = await signal.wait_for_work(timeout=0.05)
        assert result is False

    async def test_wake_all_behaves_like_wake_one_for_mono(self) -> None:
        """wake_all() equivale a wake_one() en la implementación mono."""
        signal = MonoWorkerWakeSignal()

        async def _trigger() -> None:
            await asyncio.sleep(0.01)
            signal.wake_all()

        asyncio.create_task(_trigger())
        got_wake = await signal.wait_for_work(timeout=5.0)
        assert got_wake is True


class TestWakeTiming:
    async def test_first_work_served_under_300ms(self) -> None:
        """SC-006: primer 'trabajo' disponible en < 300 ms con fake enqueue.

        Simula el flujo enqueue → wake_one → loop_drains mediante:
          1. Worker ocioso en wait_for_work().
          2. Fake enqueue llama wake_one() tras 50ms (simula commit+wake).
          3. El worker recibe el wake y 'procesa' el item.
        Mide tiempo total desde inicio del wait hasta recepción del wake.
        """
        signal = MonoWorkerWakeSignal()
        items_processed: list[str] = []

        async def fake_enqueue_and_wake() -> None:
            await asyncio.sleep(0.05)
            items_processed.append("item-1")
            signal.wake_one()

        asyncio.create_task(fake_enqueue_and_wake())

        start = time.monotonic()
        got_wake = await signal.wait_for_work(timeout=5.0)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert got_wake is True
        assert len(items_processed) == 1
        assert elapsed_ms < 300, (
            f"Primer trabajo recibido en {elapsed_ms:.1f}ms, máximo 300ms (SC-006)"
        )


class TestIdleIntegration:
    async def test_idle_interruptible_by_wake(self) -> None:
        """_idle() modificado: wake_one() interrumpe asyncio.sleep ciego."""
        from hermes.tasks.application.agent_loop_orchestrator import (  # noqa: PLC0415
            AgentLoopOrchestrator,
        )
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        from hermes.testing import FakeReasoningEngine  # noqa: PLC0415
        from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415
        from uuid import uuid4  # noqa: PLC0415

        tenant = uuid4()
        orch = AgentLoopOrchestrator(
            queue=InMemoryWorkQueue(),
            state=InMemoryAgentState(),
            engine=FakeReasoningEngine(),
            broker=FakeCapabilityBroker(),
            consent_context=ConsentContext(tenant_id=tenant, operator_id=None),
            notify_watchdog=lambda: None,
            idle_poll_s=10.0,  # largo — wake debe interrumpirlo antes
            pause_poll_s=10.0,
        )

        start = time.monotonic()

        async def _interrupt_idle() -> None:
            await asyncio.sleep(0.02)
            orch.wake_signal.wake_one()

        asyncio.create_task(_interrupt_idle())
        await orch._idle(10.0)  # type: ignore[attr-defined]
        elapsed = time.monotonic() - start

        assert elapsed < 0.3, (
            f"_idle() debió interrumpirse en < 300ms pero tardó {elapsed:.3f}s"
        )
