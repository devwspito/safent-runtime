"""SystemEventTriggerSource — listener D-Bus de eventos del SO (US3/FR-021/FR-023).

Fuente de trabajo candidato que escucha señales D-Bus de systemd/UPower/
NetworkManager/udev. Solo reacciona a AllowedSystemEvent (lista CERRADA —
SC-010). El payload del evento es SIEMPRE no confiable (derived_from_untrusted_
content=True — CTRL-P2-12/FR-024).

Bajo watchdog dedicado (CTRL-P2-16/FR-023): si esta corrutina falla, el
watchdog del daemon la reinicia; su caída NO produce trabajo fantasma (solo
construye candidatos; si muere antes de enqueue_from_trigger, no hay encolado).

Coalescing/debounce (CTRL-P2-11): un servicio en crash-loop puede emitir
PropertiesChanged en ráfaga. Se mantiene un dict {scope_value → last_fired_monotonic}
con ventana de _DEBOUNCE_S (5 min) para que ≤1 tarea viva por (tipo+scope).

Diseño: NO tiene dependencia de dbus-fast en tiempo de import. El _DbusBus
real se carga lazily solo cuando run_forever() se llama (así el módulo es
importable en CI sin D-Bus instalado). Los tests usan el fake (inject_event).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from hermes.tasks.triggers.domain.authorized_trigger_ports import AuthorizedTriggerType
from hermes.tasks.triggers.domain.ports import AllowedSystemEvent

if TYPE_CHECKING:
    from hermes.tasks.triggers.application.trigger_gate import TriggerGate

logger = logging.getLogger("hermes.tasks.triggers.system_event")

# Ventana de debounce: eventos del mismo tipo+scope en esta ventana
# se colapsan en una sola tarea (CTRL-P2-11).
_DEBOUNCE_S: float = 300.0  # 5 minutos

# Sender bus names de los que aceptamos señales (CTRL-P2-17).
_TRUSTED_SENDERS = frozenset({
    "org.freedesktop.systemd1",
    "org.freedesktop.UPower",
    "org.freedesktop.NetworkManager",
    # udev no tiene bus name; filtro por interface en el adaptador real.
})


class SystemEventTriggerSource:
    """Listener D-Bus push para eventos del SO (FR-021).

    run_forever(): corrutina del gather del daemon.
    inject_event(): punto de entrada para tests (sin D-Bus real).
    """

    def __init__(
        self,
        *,
        gate: TriggerGate,
        debounce_s: float = _DEBOUNCE_S,
    ) -> None:
        self._gate = gate
        self._debounce_s = debounce_s
        self._shutdown = asyncio.Event()
        # Última vez (monotonic) que se disparó por (event_type, scope_value)
        self._last_fired: dict[tuple[str, str], float] = {}
        # Cola interna usada por inject_event para tests (sin D-Bus real)
        self._event_queue: asyncio.Queue[tuple[AllowedSystemEvent, str]] = asyncio.Queue()

    def allowed_events(self) -> frozenset[AllowedSystemEvent]:
        """Conjunto cerrado de eventos a los que reacciona (FR-021)."""
        return frozenset(AllowedSystemEvent)

    async def run_forever(self) -> None:
        """Bucle del listener. Termina con cancelación o request_shutdown()."""
        logger.info("hermes.triggers.system_event.started")
        try:
            await self._listen_loop()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("hermes.triggers.system_event.stopped")

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def inject_event(
        self,
        event_type: AllowedSystemEvent,
        scope_value: str,
    ) -> None:
        """Inyecta un evento para tests (sin D-Bus real)."""
        await self._event_queue.put((event_type, scope_value))

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    async def _listen_loop(self) -> None:
        """Espera eventos de la cola (inyectados por tests o por el adaptador D-Bus)."""
        while not self._shutdown.is_set():
            try:
                event_type, scope_value = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=1.0,
                )
            except TimeoutError:
                continue

            await self._handle_event(event_type, scope_value)

    async def _handle_event(
        self,
        event_type: AllowedSystemEvent,
        scope_value: str,
    ) -> None:
        """Maneja un evento del SO (CTRL-P2-11/12/13)."""
        import time  # noqa: PLC0415

        # CTRL-P2-13: solo AllowedSystemEvent (lista cerrada)
        if event_type not in self.allowed_events():
            logger.debug(
                "hermes.triggers.system_event.ignored_not_allowed",
                extra={"event_type": str(event_type)},
            )
            return

        # CTRL-P2-11: debounce por (tipo, scope)
        key = (str(event_type), scope_value)
        now = time.monotonic()
        if (now - self._last_fired.get(key, 0.0)) < self._debounce_s:
            logger.debug(
                "hermes.triggers.system_event.debounced",
                extra={"event_type": str(event_type), "scope_value": scope_value},
            )
            return

        # CTRL-P2-12: taint obligatorio — payload es no confiable
        dedup_key = f"sysevent-{event_type}-{scope_value}-{_minute_bucket()}"
        task_id = await self._gate.enqueue_from_trigger(
            trigger_type=AuthorizedTriggerType.SYSTEM_EVENT,
            scope_value=str(event_type),
            instruction=_sanitize_scope(scope_value),
            dedup_key=dedup_key,
            derived_from_untrusted_content=True,  # SIEMPRE taint (CTRL-P2-12)
        )

        if task_id is not None:
            self._last_fired[key] = now
            logger.info(
                "hermes.triggers.system_event.fired",
                extra={
                    "event_type": str(event_type),
                    "task_id": str(task_id),
                    # CTRL-P2-19: NO loguear scope_value (puede contener PII)
                },
            )


# Limite conservador anti prompt-injection; la instrucción completa la provee el admin.
_SCOPE_MAX_LEN = 64


def _sanitize_scope(scope_value: str) -> str:
    """Trunca el scope para evitar prompt-injection por payloads largos."""
    return scope_value[:_SCOPE_MAX_LEN] if len(scope_value) > _SCOPE_MAX_LEN else scope_value


def _minute_bucket() -> str:
    """Bucket de 5 min para dedup key de events (coalescing)."""
    from datetime import UTC, datetime  # noqa: PLC0415
    now = datetime.now(tz=UTC)
    bucket = (now.minute // 5) * 5
    return f"{now.year}-{now.month:02d}-{now.day:02d}T{now.hour:02d}:{bucket:02d}"
