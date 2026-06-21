"""T045 — StreamBroker: fan-out de TaskStreamFrame por task_id.

Application layer. Sin I/O directo — los adapters de infraestructura
(socket Unix WS) son sus consumidores y productores.

Contratos (FR-009, FR-017, task_stream_socket_v1.md):
  - publish(): emite un frame a todos los suscriptores de task_id.
    Back-pressure best-effort: descarta deltas para clientes lentos.
    La verdad durable es el audit, no el stream.
  - subscribe(): retorna un AsyncGenerator de frames para task_id.
    Re-attach: si la tarea ya tiene un estado terminal (done/error),
    reenvía el status + done actuales (NO replay del histórico de tokens).
  - close_task(): cierra todos los suscriptores de task_id con frame DONE.

Garantías de aislamiento multi-task:
  Cada task_id tiene su propia lista de colas de suscriptor. Dos tareas
  concurrentes NO mezclan chunks (CTRL-P1-10 / contrato del socket v1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator
from uuid import UUID

from hermes.tasks.control_plane.domain.task_stream_frame import (
    TaskStreamFrame,
    delta_frame,
    done_frame,
)

logger = logging.getLogger("hermes.tasks.stream_broker")

# Capacidad máxima del buffer por suscriptor. Si se llena, el próximo delta
# se descarta (back-pressure best-effort). Los frames STATUS y DONE nunca
# se descartan (se fuerza put_nowait con capacidad extra de 1 frame urgente).
_SUBSCRIBER_BUFFER_SIZE: int = 64

# Sentinel que indica al generador del suscriptor que debe cerrarse.
_CLOSE_SENTINEL = object()


class StreamBroker:
    """Fan-out de TaskStreamFrame por task_id a suscriptores asyncio.

    Concurrencia: diseñado para un único hilo asyncio (mismo event loop que
    el daemon). Thread-safety adicional fuera de scope (no multi-process).
    """

    def __init__(self) -> None:
        # task_id -> lista de (cola, loop) por suscriptor activo. Guardamos el
        # event loop del suscriptor para entregar SIEMPRE via call_soon_threadsafe:
        # publish() puede ser invocado desde un contexto de ejecución distinto al
        # del suscriptor (p.ej. el ciclo del worker tras run_in_executor); un
        # put_nowait "a pelo" en ese caso encola el item pero NO despierta al
        # `await q.get()` del suscriptor → 0 frames. call_soon_threadsafe lo
        # agenda en el loop correcto (no-op extra si es el mismo loop).
        self._subscribers: dict[
            UUID, list[tuple[asyncio.Queue[object], asyncio.AbstractEventLoop]]
        ] = defaultdict(list)
        # Estado terminal actual por task_id (status + done para re-attach)
        self._terminal_status: dict[UUID, TaskStreamFrame] = {}
        self._terminal_done: dict[UUID, TaskStreamFrame] = {}
        # Deltas acumulados por task_id para REPLAY en re-attach. Un suscriptor
        # que conecta DESPUÉS de que la tarea terminó (caso común con modelos
        # rápidos: el ChatWorker se engancha tras completarse) debe poder leer la
        # respuesta. Sin esto, re-attach solo daba status+done (sin texto) → la UI
        # mostraba el mensaje del usuario pero ninguna respuesta. La narrativa de
        # chat es un único delta, así que el coste de memoria es la propia
        # respuesta (acotada por nº de conversaciones).
        self._terminal_deltas: dict[UUID, list[str]] = defaultdict(list)

    def publish(self, frame: TaskStreamFrame) -> None:
        """Publica un frame a todos los suscriptores de frame.task_id.

        Back-pressure: los deltas se descartan si la cola del suscriptor
        está llena. Los frames STATUS/DONE/ERROR (lifecycle) se intentan
        encolar; si la cola está llena se descarta un delta existente para
        hacer espacio (los lifecycle nunca se pierden silenciosamente).

        No bloqueante — NO usa await. Llamado desde el loop del daemon.
        """
        is_lifecycle = frame.kind.value in ("status", "done", "error")

        if is_lifecycle:
            self._record_terminal_state(frame)
        elif frame.kind.value == "delta":
            # Acumula para replay en re-attach (suscriptor que llega tarde).
            delta_text = frame.payload.get("delta", "")
            if delta_text:
                self._terminal_deltas[frame.task_id].append(delta_text)

        subscribers = self._subscribers.get(frame.task_id, [])
        for q, loop in list(subscribers):
            loop.call_soon_threadsafe(self._deliver, q, frame, is_lifecycle)

    def _deliver(
        self, q: asyncio.Queue, frame: object, is_lifecycle: bool
    ) -> None:
        """Entrega un frame a una cola — corre EN el loop del suscriptor."""
        if not q.full():
            q.put_nowait(frame)
        elif is_lifecycle:
            # Descarta el item más viejo para hacer sitio al lifecycle frame
            # (status/done/error never silent-dropped).
            with contextlib.suppress(Exception):
                q.get_nowait()
            try:
                q.put_nowait(frame)
            except Exception:  # noqa: BLE001
                logger.warning("hermes.tasks.stream_broker.lifecycle_frame_dropped")
        else:
            logger.debug("hermes.tasks.stream_broker.delta_dropped")

    async def subscribe(
        self, *, task_id: UUID
    ) -> AsyncGenerator[TaskStreamFrame, None]:
        """Suscribe al stream de task_id.

        Re-attach: si la tarea ya tiene estado terminal registrado, emite
        el status + done actuales ANTES de retornar (no replay de tokens).
        El generador termina cuando recibe el frame DONE o cuando el broker
        lo cierra explícitamente.
        """
        # Re-attach: reenvía estado actual si ya terminó (status + DELTAS + done).
        # Replay de los deltas para que un suscriptor que conecta tras completarse
        # la tarea reciba la respuesta (no solo status+done vacíos).
        if task_id in self._terminal_done:
            if task_id in self._terminal_status:
                yield self._terminal_status[task_id]
            for _delta in self._terminal_deltas.get(task_id, []):
                yield delta_frame(task_id=task_id, delta=_delta)
            yield self._terminal_done[task_id]
            return

        q: asyncio.Queue[object] = asyncio.Queue(maxsize=_SUBSCRIBER_BUFFER_SIZE)
        loop = asyncio.get_running_loop()
        entry = (q, loop)
        subscribers_list = self._subscribers[task_id]
        subscribers_list.append(entry)

        try:
            while True:
                item = await q.get()
                if item is _CLOSE_SENTINEL:
                    break
                assert isinstance(item, TaskStreamFrame)
                yield item
                if item.kind.value in ("done", "error"):
                    break
        finally:
            with contextlib.suppress(ValueError):
                subscribers_list.remove(entry)
            if not subscribers_list:
                self._subscribers.pop(task_id, None)

    def close_task(self, *, task_id: UUID, outcome: str, error: str | None = None) -> None:
        """Cierra todos los suscriptores de task_id con un frame DONE.

        Envía el sentinel de cierre a cada cola activa. Si la cola está llena
        se descarta el item más antiguo para garantizar que DONE y el sentinel
        llegan (lifecycle frames never silently dropped).
        """
        frame = done_frame(task_id=task_id, outcome=outcome, error=error)
        self._record_terminal_state(frame)

        subscribers = self._subscribers.get(task_id, [])
        for q, loop in list(subscribers):
            loop.call_soon_threadsafe(self._force_enqueue, q, frame)
            loop.call_soon_threadsafe(self._force_enqueue, q, _CLOSE_SENTINEL)

        logger.info(
            "hermes.tasks.stream_broker.task_closed",
            extra={"task_id": str(task_id), "outcome": outcome},
        )

    def subscriber_count(self, task_id: UUID) -> int:
        """Número de suscriptores activos para task_id (observabilidad/tests)."""
        return len(self._subscribers.get(task_id, []))

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _force_enqueue(self, q: asyncio.Queue, item: object) -> None:
        """Encola item descartando el más antiguo si la cola está llena."""
        if q.full():
            with contextlib.suppress(Exception):
                q.get_nowait()
        try:
            q.put_nowait(item)
        except Exception:  # noqa: BLE001
            logger.warning("hermes.tasks.stream_broker.force_enqueue_failed")

    def _record_terminal_state(self, frame: TaskStreamFrame) -> None:
        """Registra el estado terminal para re-attach."""
        if frame.kind.value == "status":
            self._terminal_status[frame.task_id] = frame
        elif frame.kind.value in ("done", "error"):
            self._terminal_done[frame.task_id] = frame
