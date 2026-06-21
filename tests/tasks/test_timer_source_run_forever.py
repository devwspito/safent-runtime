"""Regresión: el SchedulerTimerSource debe SOBREVIVIR a su propio bucle.

Bug encontrado arrancando el daemon de verdad (no lo cazó ningún test de los
1681 verdes porque todos llamaban `_tick` directo): `_sleep_interruptible` usaba
`asyncio.suppress` (que NO existe — es `contextlib.suppress`), así que la primera
vuelta del loop lanzaba AttributeError y la corrutina moría (logueando
'timer.started' -> 'timer.stopped' al instante). Con allow-list vacía es
silencioso (default-deny), pero un timer autorizado nunca dispararía.

Este test ejerce `run_forever` real con poll corto y verifica que el source NO
muere por su cuenta — solo se detiene con request_shutdown().
"""

from __future__ import annotations

import asyncio

import pytest

from hermes.tasks.triggers.application.timer_trigger_source import SchedulerTimerSource

pytestmark = pytest.mark.unit


class _EmptyRepo:
    """Allow-list vacía (default-deny): list_enabled devuelve nada."""

    async def list_enabled(self):  # noqa: ANN201
        return []


class _NoopGate:
    async def enqueue_from_trigger(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return None


async def test_run_forever_survives_first_sleep_and_keeps_ticking() -> None:
    """El timer debe seguir vivo tras varias vueltas (no morir en el 1er sleep)."""
    source = SchedulerTimerSource(gate=_NoopGate(), repo=_EmptyRepo(), poll_interval_s=0.01)
    task = asyncio.create_task(source.run_forever())

    # Dale tiempo a varias vueltas del bucle (tick + sleep interrumpible).
    await asyncio.sleep(0.1)

    # Con el bug (asyncio.suppress) la tarea ya estaría TERMINADA con AttributeError.
    assert not task.done(), (
        "el timer source murió por su cuenta — el bucle run_forever no sobrevive "
        "(regresión de asyncio.suppress -> contextlib.suppress)"
    )

    # Y debe parar limpio cuando se le pide.
    source.request_shutdown()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
    assert task.exception() is None, "run_forever no debe terminar con excepción"
