"""T016 — VO TaskStreamFrame (protocolo v1 del socket de stream de tareas).

Verifica:
- Todos los kinds del protocolo (delta, thinking_delta, tool_call, status, done, error).
- Serialización JSONL determinista: campos obligatorios presentes, sort_keys.
- Round-trip to_jsonl -> from_jsonl.
- protocol_version siempre presente en el wire.
- done con error legible (Edge Case "sin modelo": error="inference_not_configured").
- done sin error (outcome=completed).
- Deserialización falla con ValueError si faltan campos obligatorios.
- El frame es frozen (inmutable).
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from hermes.tasks.control_plane.domain.ports import StreamChunkKind
from hermes.tasks.control_plane.domain.task_stream_frame import (
    PROTOCOL_VERSION,
    TaskStreamFrame,
    delta_frame,
    done_frame,
    error_frame,
    status_frame,
    thinking_delta_frame,
    tool_call_frame,
)

pytestmark = pytest.mark.unit

_TASK_ID: UUID = UUID("12345678-1234-5678-1234-567812345678")


# ---------------------------------------------------------------------------
# Factories — todos los kinds
# ---------------------------------------------------------------------------


def test_delta_frame_kind() -> None:
    f = delta_frame(task_id=_TASK_ID, delta="hello")
    assert f.kind is StreamChunkKind.DELTA
    assert f.payload["delta"] == "hello"
    assert f.task_id == _TASK_ID
    assert f.protocol_version == PROTOCOL_VERSION


def test_thinking_delta_frame_kind() -> None:
    f = thinking_delta_frame(task_id=_TASK_ID, delta="reasoning step")
    assert f.kind is StreamChunkKind.THINKING_DELTA
    assert f.payload["delta"] == "reasoning step"


def test_tool_call_frame_kind() -> None:
    tc = {"name": "browser_click", "args": {"selector": "#submit"}}
    f = tool_call_frame(task_id=_TASK_ID, tool_call=tc)
    assert f.kind is StreamChunkKind.TOOL_CALL
    assert f.payload["tool_call"] == tc


def test_status_frame_kind() -> None:
    f = status_frame(task_id=_TASK_ID, status="in_progress")
    assert f.kind is StreamChunkKind.STATUS
    assert f.payload["status"] == "in_progress"


def test_done_frame_completed_no_error() -> None:
    f = done_frame(task_id=_TASK_ID, outcome="completed")
    assert f.kind is StreamChunkKind.DONE
    assert f.payload["outcome"] == "completed"
    assert "error" not in f.payload


def test_done_frame_failed_with_error() -> None:
    f = done_frame(
        task_id=_TASK_ID,
        outcome="failed",
        error="inference_not_configured",
    )
    assert f.kind is StreamChunkKind.DONE
    assert f.payload["outcome"] == "failed"
    assert f.payload["error"] == "inference_not_configured"


def test_done_frame_rejected() -> None:
    f = done_frame(task_id=_TASK_ID, outcome="rejected")
    assert f.payload["outcome"] == "rejected"


def test_error_frame_kind() -> None:
    f = error_frame(task_id=_TASK_ID, error="provider_5xx")
    assert f.kind is StreamChunkKind.ERROR
    assert f.payload["error"] == "provider_5xx"


# ---------------------------------------------------------------------------
# Serialización JSONL
# ---------------------------------------------------------------------------


def test_to_jsonl_is_single_line() -> None:
    f = delta_frame(task_id=_TASK_ID, delta="hi")
    line = f.to_jsonl()
    assert "\n" not in line


def test_to_jsonl_contains_required_fields() -> None:
    f = status_frame(task_id=_TASK_ID, status="pending")
    wire = json.loads(f.to_jsonl())
    assert "kind" in wire
    assert "task_id" in wire
    assert "protocol_version" in wire


def test_to_jsonl_protocol_version_in_wire() -> None:
    f = delta_frame(task_id=_TASK_ID, delta="x")
    wire = json.loads(f.to_jsonl())
    assert wire["protocol_version"] == PROTOCOL_VERSION


def test_to_jsonl_task_id_as_string() -> None:
    f = delta_frame(task_id=_TASK_ID, delta="x")
    wire = json.loads(f.to_jsonl())
    assert wire["task_id"] == str(_TASK_ID)


def test_to_jsonl_keys_are_sorted() -> None:
    """sort_keys=True garantiza serialización determinista."""
    f = done_frame(task_id=_TASK_ID, outcome="completed", error="oops")
    line = f.to_jsonl()
    keys = list(json.loads(line).keys())
    assert keys == sorted(keys)


def test_to_jsonl_no_extra_spaces() -> None:
    f = delta_frame(task_id=_TASK_ID, delta="hi")
    line = f.to_jsonl()
    assert ": " not in line  # separators=(",",":")


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_delta() -> None:
    original = delta_frame(task_id=_TASK_ID, delta="world")
    restored = TaskStreamFrame.from_jsonl(original.to_jsonl())
    assert restored.kind is original.kind
    assert restored.task_id == original.task_id
    assert restored.payload == original.payload
    assert restored.protocol_version == original.protocol_version


def test_round_trip_done_with_error() -> None:
    original = done_frame(
        task_id=_TASK_ID,
        outcome="failed",
        error="inference_not_configured",
    )
    restored = TaskStreamFrame.from_jsonl(original.to_jsonl())
    assert restored == original


def test_round_trip_tool_call() -> None:
    tc = {"name": "scroll", "args": {"direction": "down"}}
    original = tool_call_frame(task_id=_TASK_ID, tool_call=tc)
    restored = TaskStreamFrame.from_jsonl(original.to_jsonl())
    assert restored.payload["tool_call"] == tc


def test_round_trip_different_task_ids() -> None:
    id1, id2 = uuid4(), uuid4()
    f1 = status_frame(task_id=id1, status="in_progress")
    f2 = status_frame(task_id=id2, status="pending")
    r1 = TaskStreamFrame.from_jsonl(f1.to_jsonl())
    r2 = TaskStreamFrame.from_jsonl(f2.to_jsonl())
    assert r1.task_id != r2.task_id


# ---------------------------------------------------------------------------
# Deserialización — campos faltantes
# ---------------------------------------------------------------------------


def test_from_jsonl_missing_kind_raises() -> None:
    raw = json.dumps({"task_id": str(_TASK_ID), "protocol_version": 1})
    with pytest.raises(ValueError, match="kind"):
        TaskStreamFrame.from_jsonl(raw)


def test_from_jsonl_missing_task_id_raises() -> None:
    raw = json.dumps({"kind": "delta", "protocol_version": 1, "delta": "x"})
    with pytest.raises(ValueError, match="task_id"):
        TaskStreamFrame.from_jsonl(raw)


def test_from_jsonl_missing_protocol_version_raises() -> None:
    raw = json.dumps({"kind": "delta", "task_id": str(_TASK_ID), "delta": "x"})
    with pytest.raises(ValueError, match="protocol_version"):
        TaskStreamFrame.from_jsonl(raw)


def test_from_jsonl_invalid_json_raises() -> None:
    with pytest.raises(json.JSONDecodeError):
        TaskStreamFrame.from_jsonl("not-json{{{")


def test_from_jsonl_unknown_kind_raises() -> None:
    raw = json.dumps(
        {"kind": "unknown_kind", "task_id": str(_TASK_ID), "protocol_version": 1}
    )
    with pytest.raises(ValueError):
        TaskStreamFrame.from_jsonl(raw)


# ---------------------------------------------------------------------------
# Inmutabilidad
# ---------------------------------------------------------------------------


def test_task_stream_frame_is_frozen() -> None:
    f = delta_frame(task_id=_TASK_ID, delta="x")
    with pytest.raises((AttributeError, TypeError)):
        f.kind = StreamChunkKind.DONE  # type: ignore[misc]
