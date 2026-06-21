"""T050 — ChunkSinkAdapter: publica TaskStreamFrame al StreamBroker por task_id.

Adapter de la capa de infraestructura que implementa ChunkSinkPort (domain/ports.py).
Se inyecta por-tarea vía DecisionContext.metadata["chunk_sink"] para NO modificar
la firma pública de ReasoningEngine.run_cycle (Constitución I — T008 debe seguir verde).

Diseño:
  - Un chunk de texto (delta) → delta_frame → broker.publish(frame)
  - Un chunk de herramienta (tool_call) → tool_call_frame → broker.publish(frame)
  - close() → broker.close_task() → envía DONE a todos los suscriptores

Back-pressure: el StreamBroker ya descarta deltas para clientes lentos.
Este adapter NO bloquea el ciclo del daemon (publish() es síncrono en el broker).

CTRL-P1-9: PROHIBIDO loguear el cuerpo del chunk (PII). Solo task_id + kind + len.
"""

from __future__ import annotations

import logging
from uuid import UUID

from hermes.tasks.control_plane.application.stream_broker import StreamBroker
from hermes.tasks.control_plane.domain.ports import ChunkSinkPort, TaskStreamChunk
from hermes.tasks.control_plane.domain.task_stream_frame import (
    delta_frame,
    done_frame,
    status_frame,
    thinking_delta_frame,
    tool_call_frame,
)

logger = logging.getLogger("hermes.tasks.chunk_sink")


class ChunkSinkAdapter:
    """Implementa ChunkSinkPort publicando al StreamBroker.

    Se instancia una vez por tarea antes de _process(). El broker ya gestiona
    el fan-out a los suscriptores activos para ese task_id.
    """

    def __init__(self, *, broker: StreamBroker) -> None:
        self._broker = broker

    async def emit(self, *, task_id: UUID, chunk: TaskStreamChunk) -> None:
        """Publica un chunk al stream de task_id. No bloquea por back-pressure.

        CTRL-P1-9: NO se loguea el contenido del chunk — solo task_id, kind y len.
        """
        frame = _chunk_to_frame(task_id, chunk)
        self._broker.publish(frame)
        logger.debug(
            "hermes.chunk_sink.emit",
            extra={
                "task_id": str(task_id),
                "kind": chunk.kind,
                "len": len(chunk.delta or ""),
            },
        )

    async def close(
        self, *, task_id: UUID, outcome: str, error: str | None = None
    ) -> None:
        """Cierra el stream con frame DONE. Notifica a todos los suscriptores."""
        self._broker.close_task(task_id=task_id, outcome=outcome, error=error)
        logger.info(
            "hermes.chunk_sink.close",
            extra={"task_id": str(task_id), "outcome": outcome},
        )

    async def emit_status(self, *, task_id: UUID, status: str) -> None:
        """Emite un frame STATUS (ciclo de vida observable)."""
        frame = status_frame(task_id=task_id, status=status)
        self._broker.publish(frame)


# Satisface ChunkSinkPort structural check
assert isinstance(ChunkSinkAdapter.__new__(ChunkSinkAdapter), ChunkSinkPort)


def _chunk_to_frame(task_id: UUID, chunk: TaskStreamChunk):
    """Convierte un TaskStreamChunk al TaskStreamFrame correspondiente."""
    from hermes.tasks.control_plane.domain.ports import StreamChunkKind  # noqa: PLC0415

    if chunk.kind is StreamChunkKind.DELTA:
        return delta_frame(task_id=task_id, delta=chunk.delta or "")
    if chunk.kind is StreamChunkKind.THINKING_DELTA:
        return thinking_delta_frame(task_id=task_id, delta=chunk.delta or "")
    if chunk.kind is StreamChunkKind.TOOL_CALL:
        return tool_call_frame(task_id=task_id, tool_call=chunk.tool_call or {})
    if chunk.kind is StreamChunkKind.STATUS:
        return status_frame(task_id=task_id, status=chunk.status or "")
    if chunk.kind is StreamChunkKind.DONE:
        return done_frame(task_id=task_id, outcome=chunk.outcome or "done", error=chunk.error)
    # ERROR y fallback
    from hermes.tasks.control_plane.domain.task_stream_frame import error_frame  # noqa: PLC0415
    return error_frame(task_id=task_id, error=chunk.error or "unknown_error")
