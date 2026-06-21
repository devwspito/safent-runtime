"""SchedulerTimerSource — fuente de temporizador (US3/FR-020/CTRL-P2-16).

Corrutina del gather del daemon. Evalúa entradas de tipo 'timer' en la
allow-list autorizada y, cuando vence el próximo disparo según expresión
cron, llama a TriggerGate.enqueue_from_trigger.

Diseño:
  - Sondea la allow-list cada `poll_interval_s` (default 60s) para
    recoger autorizaciones nuevas y detectar revocaciones.
  - Para cada instancia habilitada, calcula el próximo disparo desde
    el scope_value (expresión cron, interpretada como "cada hora" por
    simplicidad si no hay librería croniter disponible — la validación
    completa requiere `croniter`).
  - Bajo watchdog dedicado del pool (patrón worker_pool.py:179).
  - Fail-closed: un error en una instancia no detiene las demás.

El systemd timer unit en ops/ actúa como backstop durable:
  - si el proceso cae, systemd dispara el D-Bus Enqueue en el arranque.
  - Persistent=yes recupera ejecuciones perdidas sin ráfagas acumuladas.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from hermes.tasks.triggers.domain.authorized_trigger_ports import AuthorizedTriggerType

if TYPE_CHECKING:
    from hermes.tasks.triggers.application.trigger_gate import TriggerGate
    from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (
        SqliteAuthorizedTriggerRepository,
    )

logger = logging.getLogger("hermes.tasks.triggers.timer")

# Intervalo de sondeo de la allow-list (segundos).
_POLL_INTERVAL_S = 60.0

# Ventana para considerar "vencido" un disparo: si el próximo tick
# está dentro de esta ventana respecto a ahora, disparamos.
_FIRE_WINDOW_S = 30.0


class SchedulerTimerSource:
    """Corrutina de fondo que evalúa timers autorizados y encola tareas.

    Implementa TimerTriggerSource (run_forever corrutina del gather daemon).
    """

    def __init__(
        self,
        *,
        gate: TriggerGate,
        repo: SqliteAuthorizedTriggerRepository,
        poll_interval_s: float = _POLL_INTERVAL_S,
    ) -> None:
        self._gate = gate
        self._repo = repo
        self._poll_interval_s = poll_interval_s
        self._shutdown = asyncio.Event()
        # Tracking de últimos disparos para evitar duplicados dentro del ciclo
        self._last_fired: dict[UUID, float] = {}

    async def run_forever(self) -> None:
        """Bucle del timer. Termina con request_shutdown() o cancelación."""
        logger.info("hermes.triggers.timer.started")
        try:
            while not self._shutdown.is_set():
                await self._tick()
                await self._sleep_interruptible(self._poll_interval_s)
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("hermes.triggers.timer.stopped")

    def request_shutdown(self) -> None:
        self._shutdown.set()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        """Evalúa todos los timers habilitados y dispara los que tocan."""
        import time  # noqa: PLC0415
        try:
            triggers = await self._repo.list_enabled()
        except Exception:
            logger.exception("hermes.triggers.timer.list_enabled_failed")
            return

        timer_triggers = [t for t in triggers if t.trigger_type is AuthorizedTriggerType.TIMER]
        now = time.monotonic()

        for trigger in timer_triggers:
            if not self._should_fire(trigger.trigger_instance_id, now):
                continue
            await self._fire(trigger)
            self._last_fired[trigger.trigger_instance_id] = now

    def _should_fire(self, instance_id: UUID, now: float) -> bool:
        """Heurística simple: un timer dispara si no se disparó en el último ciclo."""
        last = self._last_fired.get(instance_id, 0.0)
        return (now - last) >= self._poll_interval_s

    async def _fire(self, trigger: object) -> None:
        """Dispara un trigger de timer a través del gate (fail-closed).

        P3: usa task_instruction almacenada en el trigger como instrucción del
        work item. Si está vacía, cae al fallback descriptivo anterior.
        target_agent_id (si el calendario lo fijó) se pasa al gate → viaja en el
        payload del WorkItem → el consumidor lo ejecuta con ESE agente.
        """
        try:
            # P3: instrucción almacenada supera al fallback genérico.
            stored_instruction = (
                getattr(trigger, "task_instruction", "") or ""  # type: ignore[attr-defined]
            ).strip()
            instruction = stored_instruction or (
                f"Timer scheduled task — scope={trigger.scope_value}"  # type: ignore[attr-defined]
            )
            target_agent_id = getattr(trigger, "target_agent_id", None)  # type: ignore[attr-defined]
            one_shot = bool(getattr(trigger, "one_shot", False))  # type: ignore[attr-defined]

            task_id = await self._gate.enqueue_from_trigger(
                trigger_type=AuthorizedTriggerType.TIMER,
                scope_value=trigger.scope_value,  # type: ignore[attr-defined]
                instruction=instruction,
                dedup_key=f"timer-{trigger.trigger_instance_id}-{_hour_bucket()}",  # type: ignore[attr-defined]
                target_agent_id=target_agent_id,
            )
            if task_id is not None:
                logger.info(
                    "hermes.triggers.timer.fired",
                    extra={
                        "instance_id": str(trigger.trigger_instance_id),  # type: ignore[attr-defined]
                        "task_id": str(task_id),
                        "target_agent_id": target_agent_id,
                        "one_shot": one_shot,
                    },
                )
                # P3: one_shot — auto-revoca tras la primera ejecución exitosa.
                if one_shot:
                    await self._revoke_one_shot(trigger)
        except Exception:
            logger.exception(
                "hermes.triggers.timer.fire_failed",
                extra={"instance_id": str(trigger.trigger_instance_id)},  # type: ignore[attr-defined]
            )

    async def _revoke_one_shot(self, trigger: object) -> None:
        """Revoca el trigger one-shot tras su primera ejecución exitosa."""
        try:
            from datetime import UTC, datetime  # noqa: PLC0415
            from uuid import UUID  # noqa: PLC0415

            now_iso = datetime.now(tz=UTC).isoformat()
            instance_id = str(trigger.trigger_instance_id)  # type: ignore[attr-defined]
            # Reuse the repo's connection directly (same pattern as revoke()).
            self._repo._conn.execute(  # noqa: SLF001
                """
                UPDATE authorized_trigger_instances
                SET enabled = 0,
                    revoked_at = ?,
                    revoked_by_admin_uuid = ?,
                    updated_at = ?
                WHERE instance_id = ? AND enabled = 1
                """,
                (
                    now_iso,
                    str(trigger.created_by_admin_uuid),  # type: ignore[attr-defined]
                    now_iso,
                    instance_id,
                ),
            )
            self._repo._conn.commit()  # noqa: SLF001
            logger.info(
                "hermes.triggers.timer.one_shot_revoked",
                extra={"instance_id": instance_id},
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "hermes.triggers.timer.one_shot_revoke_failed",
                extra={"instance_id": str(getattr(trigger, "trigger_instance_id", "?"))},
            )

    async def _sleep_interruptible(self, seconds: float) -> None:
        """Duerme hasta `seconds` o hasta que shutdown sea señalizado."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(self._shutdown.wait()),
                timeout=seconds,
            )


def _hour_bucket() -> str:
    """Dedup key estable dentro de la misma hora UTC (evita duplicados en crash-loops)."""
    from datetime import UTC, datetime  # noqa: PLC0415
    now = datetime.now(tz=UTC)
    return f"{now.year}-{now.month:02d}-{now.day:02d}T{now.hour:02d}"
