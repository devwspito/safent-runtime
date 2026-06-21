"""VO TaskStreamFrame — frame del protocolo de stream de tareas v1.

Conforme a contracts/task_stream_socket_v1.md. Serialización JSONL determinista
(claves ordenadas, sin espacios extra). Domain layer: cero framework, cero I/O.

Invariante: `protocol_version` siempre presente en el frame serializado.
El primer frame del servidor SIEMPRE es kind=STATUS con protocol_version=1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from hermes.tasks.control_plane.domain.ports import StreamChunkKind

# Versión del protocolo de stream (incrementar si se rompe el contrato).
PROTOCOL_VERSION: int = 1


@dataclass(frozen=True, slots=True)
class TaskStreamFrame:
    """Frame JSONL del stream de tareas (socket Unix WS, task_stream_socket_v1.md).

    Campos:
      kind            — tipo del frame (delta|thinking_delta|tool_call|status|done|error).
      task_id         — UUID de la tarea propietaria del stream.
      payload         — campos específicos del kind (delta, tool_call, status, outcome, error).
      protocol_version — versión del protocolo; se incluye en todos los frames para
                         permitir que el cliente detecte incompatibilidades sin inspeccionar
                         el kind.

    Serialización: `to_jsonl()` produce una línea JSON con claves ordenadas
    lexicográficamente (determinista, reproducible en tests). Sin trailing newline.
    """

    kind: StreamChunkKind
    task_id: UUID
    payload: dict[str, Any] = field(default_factory=dict)
    protocol_version: int = PROTOCOL_VERSION

    def to_jsonl(self) -> str:
        """Serializa el frame a una línea JSON sin newline final.

        La serialización es determinista: claves ordenadas, sin espacios extra.
        Segura para JSONL (una línea = un frame).
        """
        obj = _build_wire_object(self)
        return json.dumps(obj, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_jsonl(cls, line: str) -> TaskStreamFrame:
        """Deserializa una línea JSONL. Inverso de `to_jsonl()`.

        Raises:
            ValueError: si faltan campos obligatorios (kind, task_id, protocol_version).
            json.JSONDecodeError: si la línea no es JSON válido.
        """
        raw = json.loads(line)
        return _parse_wire_object(raw)


# ---------------------------------------------------------------------------
# Constructores de conveniencia (un factory por kind)
# ---------------------------------------------------------------------------


def delta_frame(*, task_id: UUID, delta: str) -> TaskStreamFrame:
    """Fragmento de respuesta textual del agente."""
    return TaskStreamFrame(
        kind=StreamChunkKind.DELTA,
        task_id=task_id,
        payload={"delta": delta},
    )


def thinking_delta_frame(*, task_id: UUID, delta: str) -> TaskStreamFrame:
    """Fragmento de bloque de razonamiento (separado de la respuesta final)."""
    return TaskStreamFrame(
        kind=StreamChunkKind.THINKING_DELTA,
        task_id=task_id,
        payload={"delta": delta},
    )


def tool_call_frame(*, task_id: UUID, tool_call: dict[str, Any]) -> TaskStreamFrame:
    """El agente propone/ejecuta una herramienta (nombre + args redactados)."""
    return TaskStreamFrame(
        kind=StreamChunkKind.TOOL_CALL,
        task_id=task_id,
        payload={"tool_call": tool_call},
    )


def status_frame(*, task_id: UUID, status: str) -> TaskStreamFrame:
    """Transición de ciclo de vida observable. Primer frame SIEMPRE es status."""
    return TaskStreamFrame(
        kind=StreamChunkKind.STATUS,
        task_id=task_id,
        payload={"status": status},
    )


def done_frame(
    *, task_id: UUID, outcome: str, error: str | None = None
) -> TaskStreamFrame:
    """Fin del stream. outcome ∈ completed|failed|rejected."""
    p: dict[str, Any] = {"outcome": outcome}
    if error is not None:
        p["error"] = error
    return TaskStreamFrame(
        kind=StreamChunkKind.DONE,
        task_id=task_id,
        payload=p,
    )


def error_frame(*, task_id: UUID, error: str) -> TaskStreamFrame:
    """Error de transporte/ciclo no terminal (puede ir seguido de done)."""
    return TaskStreamFrame(
        kind=StreamChunkKind.ERROR,
        task_id=task_id,
        payload={"error": error},
    )


# ---------------------------------------------------------------------------
# Helpers de serialización (privados)
# ---------------------------------------------------------------------------


def _build_wire_object(frame: TaskStreamFrame) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "kind": frame.kind,
        "protocol_version": frame.protocol_version,
        "task_id": str(frame.task_id),
    }
    obj.update(frame.payload)
    return obj


def _parse_wire_object(raw: dict[str, Any]) -> TaskStreamFrame:
    for required in ("kind", "task_id", "protocol_version"):
        if required not in raw:
            raise ValueError(f"Campo obligatorio ausente en frame: {required!r}")

    kind = StreamChunkKind(raw["kind"])
    task_id = UUID(raw["task_id"])
    protocol_version = int(raw["protocol_version"])

    payload_keys = set(raw) - {"kind", "task_id", "protocol_version"}
    payload = {k: raw[k] for k in payload_keys}

    return TaskStreamFrame(
        kind=kind,
        task_id=task_id,
        payload=payload,
        protocol_version=protocol_version,
    )
