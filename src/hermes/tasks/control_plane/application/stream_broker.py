"""T045 — StreamBroker: fan-out de TaskStreamFrame por task_id.

Application layer. Sin I/O directo — los adapters de infraestructura
(socket Unix WS) son sus consumidores y productores.

Contratos (FR-009, FR-017, task_stream_socket_v1.md):
  - publish(): emite un frame a todos los suscriptores de task_id.
    Back-pressure best-effort: descarta deltas para clientes lentos.
    La verdad durable es el audit, no el stream.
  - subscribe(): retorna un AsyncGenerator de frames para task_id.
    Re-attach "tipo cloud": replaya el LOG ORDENADO COMPLETO del run hasta el
    momento (status + tool_calls + thinking + tokens) y luego sigue en vivo, sin
    importar si la tarea sigue corriendo o ya terminó.
  - close_task(): cierra todos los suscriptores de task_id con frame DONE.

Garantías de aislamiento multi-task:
  Cada task_id tiene su propia lista de colas de suscriptor. Dos tareas
  concurrentes NO mezclan chunks (CTRL-P1-10 / contrato del socket v1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict, defaultdict
from collections.abc import AsyncGenerator
from dataclasses import replace
from uuid import UUID

from hermes.tasks.control_plane.domain.task_stream_frame import (
    TaskStreamFrame,
    done_frame,
)

logger = logging.getLogger("hermes.tasks.stream_broker")

# Capacidad máxima del buffer por suscriptor. Si se llena, el próximo delta
# se descarta (back-pressure best-effort). Los frames STATUS y DONE nunca
# se descartan (se fuerza put_nowait con capacidad extra de 1 frame urgente).
_SUBSCRIBER_BUFFER_SIZE: int = 64

# Sentinel que indica al generador del suscriptor que debe cerrarse.
_CLOSE_SENTINEL = object()

# Tope del log de replay por task_id (acota memoria; si se supera, se descartan los
# frames más antiguos). Cubre tareas largas con muchos tool_calls + tokens. La verdad
# durable del resultado final es el espejo de conversación / audit, no este log.
_MAX_REPLAY_FRAMES: int = 6000

# Cuántas tareas TERMINADAS se conservan en RAM (su replay/seq) para reconexiones
# tardías. Más allá, se evicta la más antigua. Sin esto, _replay_log/_terminal_done/
# _seq crecían por task_id PARA SIEMPRE (fuga de memoria sin techo en la sesión).
_MAX_TERMINAL_TASKS: int = 256


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
        # Frame DONE/ERROR terminal por task_id (cierra el replay).
        self._terminal_done: dict[UUID, TaskStreamFrame] = {}
        # LOG DE REPLAY ORDENADO por task_id: TODOS los frames no-terminales en el orden
        # exacto en que ocurrieron (status, tool_call, thinking_delta, delta). Esto es lo
        # que hace el chat "tipo cloud/Codex": un cliente que reconecta/refresca/cambia de
        # sesión replaya el run ENTERO hasta el momento — los pasos de las tools Y el texto,
        # no solo la respuesta final — y sigue en vivo. Antes solo se acumulaban los deltas
        # de texto → al reconectar a media tarea (p.ej. mientras navega la web) no se veían
        # las tools ni nada → pantalla en blanco. Acotado por _MAX_REPLAY_FRAMES.
        self._replay_log: dict[UUID, list[TaskStreamFrame]] = defaultdict(list)
        # Contador de secuencia monótono por task_id. Cada frame emitido recibe un
        # `seq` creciente en su payload. Así un cliente que re-engancha (reconexión WS
        # o refresco) DEDUPLICA: descarta frames con seq <= el último que ya aplicó, en
        # vez de re-añadirlos sobre lo ya pintado (causa raíz de los mensajes duplicados).
        # El replay sigue siendo el run completo; el seq solo evita la doble aplicación.
        self._seq: dict[UUID, int] = defaultdict(int)
        # Orden de finalización (LRU) para evictar el estado de tareas terminadas y
        # acotar la memoria (ver _record_terminal / _MAX_TERMINAL_TASKS).
        self._terminal_order: OrderedDict[UUID, None] = OrderedDict()

    def _record_terminal(self, task_id: UUID) -> None:
        """Marca la tarea como terminada y evicta el estado de las más antiguas.

        Un suscriptor activo ya tomó su snapshot de replay al subscribe, así que
        evictar después no afecta a un replay en curso. Acota memoria a (tareas
        vivas) + (últimas _MAX_TERMINAL_TASKS terminadas)."""
        self._terminal_order.pop(task_id, None)
        self._terminal_order[task_id] = None
        while len(self._terminal_order) > _MAX_TERMINAL_TASKS:
            old, _ = self._terminal_order.popitem(last=False)
            self._replay_log.pop(old, None)
            self._terminal_done.pop(old, None)
            self._seq.pop(old, None)
            self._subscribers.pop(old, None)

    def _stamp_seq(self, frame: TaskStreamFrame) -> TaskStreamFrame:
        """Copia el frame con un `seq` monótono por task_id en su payload."""
        self._seq[frame.task_id] += 1
        return replace(
            frame, payload={**frame.payload, "seq": self._seq[frame.task_id]}
        )

    def publish(self, frame: TaskStreamFrame) -> None:
        """Publica un frame a todos los suscriptores de frame.task_id.

        Back-pressure: los deltas se descartan si la cola del suscriptor
        está llena. Los frames STATUS/DONE/ERROR (lifecycle) se intentan
        encolar; si la cola está llena se descarta un delta existente para
        hacer espacio (los lifecycle nunca se pierden silenciosamente).

        No bloqueante — NO usa await. Llamado desde el loop del daemon.
        """
        frame = self._stamp_seq(frame)
        kind = frame.kind.value
        is_lifecycle = kind in ("status", "done", "error")

        if kind in ("done", "error"):
            self._terminal_done[frame.task_id] = frame
            self._record_terminal(frame.task_id)
        else:
            # status, tool_call, thinking_delta, delta → log de replay ORDENADO (acotado).
            log = self._replay_log[frame.task_id]
            log.append(frame)
            if len(log) > _MAX_REPLAY_FRAMES:
                del log[: len(log) - _MAX_REPLAY_FRAMES]

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
        """Suscribe al stream de task_id, con REPLAY DURABLE en re-attach.

        Re-attach (FIX 2026-06-26 — el chat se quedaba en blanco al refrescar a media
        tarea): SIEMPRE reproduce el estado acumulado ANTES de los frames en vivo — el
        status actual + TODOS los deltas emitidos hasta ahora + (si ya terminó) el done.
        Antes, el replay solo ocurría si la tarea YA había terminado; un re-attach a
        MITAD de una tarea larga entraba directo por la rama viva y solo veía frames
        FUTUROS → se perdía todo lo ya streameado → la UI mostraba el mensaje del usuario
        sin respuesta. Ahora un cliente que refresca/reconecta reconstruye la respuesta
        hasta el momento y continúa en vivo, sin importar si la tarea sigue o terminó.

        Sin duplicados: tomamos el snapshot del replay ANTES de registrar la cola, así un
        delta que llegue en la micro-ventana register↔snapshot no se replica (a lo sumo se
        pierde, caso raro que el poll del espejo de conversación del cliente cubre). El
        generador termina al recibir DONE/ERROR o al cerrarse el broker.
        """
        q: asyncio.Queue[object] = asyncio.Queue(maxsize=_SUBSCRIBER_BUFFER_SIZE)
        loop = asyncio.get_running_loop()
        entry = (q, loop)
        subscribers_list = self._subscribers[task_id]

        # Snapshot del catch-up ANTES de registrar la cola (evita duplicar un frame en
        # la ventana register↔snapshot). Los frames publicados DESPUÉS del append entran
        # por la cola (índice >= len(replay)) → no solapan con el replay.
        replay = list(self._replay_log.get(task_id, []))
        replay_done = self._terminal_done.get(task_id)
        subscribers_list.append(entry)

        try:
            # Catch-up: el run ENTERO hasta ahora — status + tool_calls + thinking + texto,
            # en el orden exacto en que ocurrieron. Esto da el comportamiento "tipo cloud":
            # reconectar/refrescar/cambiar de sesión reconstruye toda la actividad viva.
            for _frame in replay:
                yield _frame
            if replay_done is not None:
                yield replay_done
                return  # la tarea ya terminó — run completo replayed
            # Vivo: frames publicados DESPUÉS del registro de la cola.
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
        frame = self._stamp_seq(done_frame(task_id=task_id, outcome=outcome, error=error))
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
        """Registra el frame terminal (done/error) para re-attach. Los demás frames
        (status incluido) van al log de replay ordenado en publish()."""
        if frame.kind.value in ("done", "error"):
            self._terminal_done[frame.task_id] = frame
