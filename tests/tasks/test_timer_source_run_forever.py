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
    """Allow-list vacía (default-deny): el timer lee el estado nativo (trigger +
    last_run_at) vía list_triggers_with_last_run; sin triggers devuelve nada."""

    def list_triggers_with_last_run(self):  # noqa: ANN201
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


# ---------------------------------------------------------------------------
# Regresión 2026-06-28 (cazado planificando una tarea LIVE +5min):
# `_should_fire` ignoraba por completo la expresión cron y disparaba CADA
# trigger habilitado en el primer tick. Una tarea para las 17:26 disparó a los 7s.
# El fix: `_due_slot` evalúa el cron con el vocabulario único `prev_fire` (el MISMO
# que el control plane usa con next_fire — NO se duplica croniter) contra un floor
# que es el `last_run_at` PERSISTIDO del nativo (created_at del agent_task más
# reciente), no un dict en memoria. Así: no dispara antes de tiempo, no re-dispara
# el mismo slot, no backfillea ráfagas, y es correcto tras restart.
# ---------------------------------------------------------------------------

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from hermes.tasks.triggers.domain.authorized_trigger_ports import AuthorizedTriggerType


def _timer(cron: str, authorized_at: datetime, **kw):  # noqa: ANN003, ANN201
    return SimpleNamespace(
        trigger_instance_id=kw.get("instance_id", uuid4()),
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value=cron,
        authorized_at=authorized_at,
        task_instruction=kw.get("task_instruction", "escribe proof.txt"),
        target_agent_id=kw.get("target_agent_id"),
        one_shot=kw.get("one_shot", False),
    )


def _src():  # noqa: ANN202
    return SchedulerTimerSource(gate=_NoopGate(), repo=_EmptyRepo(), poll_interval_s=0.01)


def test_due_slot_does_not_fire_before_scheduled_time() -> None:
    """El bug: disparaba a los 7s. Ahora NO dispara antes del slot del cron."""
    src = _src()
    auth = datetime(2026, 6, 28, 17, 21, 38, tzinfo=UTC)
    trig = _timer("26 17 * * *", auth)  # diario a las 17:26
    now = datetime(2026, 6, 28, 17, 21, 45, tzinfo=UTC)  # 7s después de crear
    assert src._due_slot(trig, None, now) is None  # noqa: SLF001


def test_due_slot_fires_at_scheduled_slot() -> None:
    src = _src()
    auth = datetime(2026, 6, 28, 17, 21, 38, tzinfo=UTC)
    trig = _timer("26 17 * * *", auth)
    now = datetime(2026, 6, 28, 17, 26, 30, tzinfo=UTC)
    slot = src._due_slot(trig, None, now)  # noqa: SLF001
    assert slot == datetime(2026, 6, 28, 17, 26, 0, tzinfo=UTC)


def test_due_slot_does_not_refire_same_slot() -> None:
    src = _src()
    auth = datetime(2026, 6, 28, 17, 21, 38, tzinfo=UTC)
    trig = _timer("26 17 * * *", auth)
    now = datetime(2026, 6, 28, 17, 26, 30, tzinfo=UTC)
    slot = src._due_slot(trig, None, now)  # noqa: SLF001
    assert slot == datetime(2026, 6, 28, 17, 26, 0, tzinfo=UTC)
    # Tras disparar, el NATIVO persiste last_run_at = created_at del agent_task
    # (≈ ahora). El siguiente poll lo lee como floor y NO re-dispara el mismo slot.
    last_run_at = "2026-06-28T17:26:31+00:00"
    later = datetime(2026, 6, 28, 17, 27, 30, tzinfo=UTC)
    assert src._due_slot(trig, last_run_at, later) is None  # noqa: SLF001


def test_due_slot_fires_new_slot_after_last_run() -> None:
    """Un slot POSTERIOR al last_run_at persistido SÍ dispara (recurrencia normal)."""
    src = _src()
    auth = datetime(2026, 6, 28, 16, 0, 0, tzinfo=UTC)
    trig = _timer("*/5 * * * *", auth)  # cada 5 min
    last_run_at = "2026-06-28T17:25:01+00:00"  # disparó el slot 17:25
    now = datetime(2026, 6, 28, 17, 30, 10, tzinfo=UTC)
    slot = src._due_slot(trig, last_run_at, now)  # noqa: SLF001
    assert slot == datetime(2026, 6, 28, 17, 30, 0, tzinfo=UTC)


def test_due_slot_no_refire_after_restart_with_persisted_floor() -> None:
    """Restart-safe: con el dict en memoria perdido, el floor persistido (last_run_at)
    impide re-disparar un slot ya ejecutado — el bug que un floor en memoria abriría."""
    src = _src()
    auth = datetime(2026, 6, 28, 16, 0, 0, tzinfo=UTC)
    trig = _timer("0 * * * *", auth)  # cada hora en punto
    last_run_at = "2026-06-28T17:00:02+00:00"  # ya disparó el slot 17:00 (y completó)
    now = datetime(2026, 6, 28, 17, 30, 0, tzinfo=UTC)  # tras "restart"
    assert src._due_slot(trig, last_run_at, now) is None  # noqa: SLF001


def test_due_slot_no_backfill_burst_after_downtime() -> None:
    """Daemon caído que se perdió varios slots dispara SOLO el más reciente."""
    src = _src()
    auth = datetime(2026, 6, 28, 16, 0, 0, tzinfo=UTC)
    trig = _timer("*/5 * * * *", auth)  # cada 5 min
    now = datetime(2026, 6, 28, 17, 26, 30, tzinfo=UTC)  # se perdió 17:05..17:25
    slot = src._due_slot(trig, None, now)  # noqa: SLF001
    assert slot == datetime(2026, 6, 28, 17, 25, 0, tzinfo=UTC)  # solo el último


def test_due_slot_wildcard_scope_never_auto_fires() -> None:
    """scope '*' es wildcard de admin (no calendario) → nunca dispara por reloj."""
    src = _src()
    auth = datetime(2026, 6, 28, 17, 0, 0, tzinfo=UTC)
    trig = _timer("*", auth)
    now = datetime(2026, 6, 28, 18, 0, 0, tzinfo=UTC)
    assert src._due_slot(trig, None, now) is None  # noqa: SLF001


def test_due_slot_invalid_cron_fails_closed() -> None:
    src = _src()
    auth = datetime(2026, 6, 28, 17, 0, 0, tzinfo=UTC)
    trig = _timer("not a cron", auth)
    now = datetime(2026, 6, 28, 18, 0, 0, tzinfo=UTC)
    assert src._due_slot(trig, None, now) is None  # noqa: SLF001


def test_due_slot_handles_naive_authorized_at() -> None:
    """Filas viejas con authorized_at naive no deben crashear la comparación."""
    src = _src()
    auth_naive = datetime(2026, 6, 28, 17, 21, 38)  # sin tz
    trig = _timer("26 17 * * *", auth_naive)
    now = datetime(2026, 6, 28, 17, 26, 30, tzinfo=UTC)
    slot = src._due_slot(trig, None, now)  # noqa: SLF001
    assert slot == datetime(2026, 6, 28, 17, 26, 0, tzinfo=UTC)


async def test_fire_dedup_key_anchored_to_slot() -> None:
    """El dedup_key debe anclarse al slot (idempotente entre polls/crash-loops)."""
    captured: dict = {}

    class _CapturingGate:
        async def enqueue_from_trigger(self, **kwargs):  # noqa: ANN003, ANN201
            captured.update(kwargs)
            return uuid4()

    src = SchedulerTimerSource(gate=_CapturingGate(), repo=_EmptyRepo(), poll_interval_s=0.01)
    auth = datetime(2026, 6, 28, 17, 21, 38, tzinfo=UTC)
    trig = _timer("26 17 * * *", auth, task_instruction="escribe proof.txt")
    slot = datetime(2026, 6, 28, 17, 26, 0, tzinfo=UTC)
    await src._fire(trig, slot)  # noqa: SLF001
    assert captured["dedup_key"].endswith(slot.isoformat())
    assert captured["instruction"] == "escribe proof.txt"
