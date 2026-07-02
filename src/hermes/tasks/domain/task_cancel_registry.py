"""Per-task cancellation registry — cooperative stop of a running task.

A task's reasoning cycle runs `run_conversation` as a BLOCKING call inside an
executor thread; a Python thread cannot be force-killed. So cancellation is
COOPERATIVE: the operator's `CancelTask` verb marks a task_id here, and the
per-token stream callback (running in that executor thread) polls this registry
and raises `OperationCancelled` to unwind the cycle, which the orchestrator then
marks CANCELLED and closes the stream.

This is a process-local singleton shared by:
  - the D-Bus wiring (writes the flag from the async loop thread), and
  - the nous_engine stream callback + orchestrator (read the flag from the
    executor / loop threads).
Thread-safe via a plain threading.Lock (NOT asyncio.Lock) precisely because the
readers run in executor threads, not the event loop.
"""

from __future__ import annotations

import threading
from uuid import UUID


class OperationCancelled(Exception):
    """Raised inside the reasoning cycle when the operator cancelled the task."""


class TaskCancelRegistry:
    """Process-local set of task_ids the operator asked to cancel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reasons: dict[UUID, str] = {}

    def request_cancel(self, task_id: UUID, *, reason: str = "") -> None:
        with self._lock:
            self._reasons[task_id] = reason or "Detenida por el operador"

    def is_cancelled(self, task_id: UUID) -> bool:
        with self._lock:
            return task_id in self._reasons

    def reason(self, task_id: UUID) -> str:
        with self._lock:
            return self._reasons.get(task_id, "Detenida por el operador")

    def clear(self, task_id: UUID) -> None:
        """Remove the flag once the task reached a terminal state."""
        with self._lock:
            self._reasons.pop(task_id, None)


_REGISTRY = TaskCancelRegistry()


def get_cancel_registry() -> TaskCancelRegistry:
    """Return the process-wide cancel registry singleton."""
    return _REGISTRY
