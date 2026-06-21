"""T047 — WorkerWakeSignal: wake-on-enqueue para el loop autónomo.

Implementación mono-worker sobre asyncio.Event. La interfaz está abstraída
desde ya (WorkerWakeSignal Protocol en control_plane/domain/ports.py) para
que PIEZA 4 (pool de N workers) pueda sustituir la implementación sin
re-refactorizar la señalización.

Garantía de orden (CTRL-P1-12):
  El enqueue DEBE llamar wake_one() DESPUÉS de que el item esté comprometido
  en la cola. La secuencia commit-then-wake está documentada aquí; el cableado
  enqueue→wake lo realiza el caller (ControlPlanePort.enqueue en T040/T048).

SC-006: con el agente ocioso, wait_for_work() retorna en < 300 ms desde
el wake — libre de latencia de la cola (el primer delta/status puede salir
inmediatamente tras el claim).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("hermes.tasks.wake_signal")


class MonoWorkerWakeSignal:
    """Implementación mono-worker de WorkerWakeSignal (asyncio.Event interno).

    Thread-safe dentro de un único hilo asyncio. Para pools multi-worker
    (PIEZA 4) sustituir por una implementación basada en asyncio.Condition
    o semáforo sin cambiar la interfaz del caller.

    Idempotente: múltiples wake_one() antes de wait_for_work() equivalen
    a un único despertar (comportamiento de asyncio.Event).
    """

    def __init__(self) -> None:
        self._event: asyncio.Event = asyncio.Event()

    def wake_one(self) -> None:
        """Despierta al worker en idle. Idempotente (varios wakes = un drain).

        Llamado por ControlPlanePort.enqueue TRAS el commit del item en la
        cola — NUNCA antes (orden estricto commit-then-wake, CTRL-P1-12).
        """
        self._event.set()
        logger.debug("hermes.tasks.wake_signal.wake_one")

    def wake_all(self) -> None:
        """Alias semánticamente explícito para el mono-worker (= wake_one).

        PIEZA 4 sobreescribirá esto para despertar N workers.
        """
        self._event.set()
        logger.debug("hermes.tasks.wake_signal.wake_all")

    async def wait_for_work(self, *, timeout: float) -> bool:
        """Espera hasta wake_one() o timeout.

        Returns:
            True  — llegó un wake (hay trabajo potencial).
            False — expiró el timeout (vuelta de poll ociosa).

        Tras retornar, resetea el event para que el próximo wait_for_work
        pueda bloquearse de nuevo (comportamiento one-shot del Event).
        """
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            self._event.clear()
            return True
        except TimeoutError:
            return False
