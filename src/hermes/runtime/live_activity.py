"""Process-wide live-activity registry for the Nous engine.

Maps task_id (str) → {agent_id, tool, since} for every in-flight tool dispatch.
Thread-safe: the emitter is called from an executor thread; readers run on the
asyncio event loop.

API
---
record(task_id, agent_id, tool)  — call before each tool dispatch.
clear(task_id)                   — call in the task-lifecycle finally block.
snapshot()                       — returns a list[dict] safe to serialise.

Design constraints:
- Dependency-free (stdlib only).
- Never raises: all mutations are fail-soft so a registry error can never
  interrupt tool execution or task lifecycle.
- Never fabricates: callers supply real values from the engine; this module
  stores exactly what it receives.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("hermes.runtime.live_activity")

_lock: threading.Lock = threading.Lock()
_registry: dict[str, dict[str, Any]] = {}


def record(task_id: str, agent_id: str, tool: str) -> None:
    """Record the currently dispatching tool for a task.

    Overwrites any prior entry for the same task_id so only the
    most-recent tool is visible (one tool at a time per task).
    Fail-soft: logs at DEBUG and swallows all exceptions.
    """
    try:
        entry = {
            "agent_id": agent_id,
            "tool": tool,
            "since": datetime.now(tz=UTC).isoformat(),
        }
        with _lock:
            _registry[task_id] = entry
    except Exception:  # noqa: BLE001 — must never crash tool dispatch
        logger.debug(
            "hermes.live_activity.record_failed task_id=%s tool=%s",
            task_id,
            tool,
        )


def clear(task_id: str) -> None:
    """Remove the entry for a completed/failed task.

    No-op if the task_id was never recorded. Fail-soft.
    """
    try:
        with _lock:
            _registry.pop(task_id, None)
    except Exception:  # noqa: BLE001
        logger.debug("hermes.live_activity.clear_failed task_id=%s", task_id)


def snapshot() -> list[dict[str, str]]:
    """Return a point-in-time copy of all in-flight tool entries.

    Each item: {"task_id": str, "agent_id": str, "tool": str, "since": str (ISO-8601)}.
    task_id is the conversation/task this tool belongs to — callers (e.g. the chat
    view) MUST filter by it so concurrent tasks/agents never cross-contaminate one
    conversation's live indicator. Returns an empty list on error.
    """
    try:
        with _lock:
            return [
                {"task_id": k, "agent_id": v["agent_id"], "tool": v["tool"], "since": v["since"]}
                for k, v in _registry.items()
            ]
    except Exception:  # noqa: BLE001
        logger.debug("hermes.live_activity.snapshot_failed")
        return []
