"""Process-wide live-activity registry for the Nous engine.

Maps task_id (str) → {agent_id, tool, since} for every in-flight tool dispatch.
Thread-safe: the emitter is called from an executor thread; readers run on the
asyncio event loop.

API
---
record(task_id, agent_id, tool)                  — call before each tool dispatch.
clear(task_id)                                    — call in the task-lifecycle finally block.
snapshot()                                        — returns a list[dict] safe to serialise.
record_delegation(task_id, from_id, to_id, label) — call when the orchestrator hands a
                                                     task off to a specialist via delegate_task.
snapshot_delegations()                            — returns a list[dict] of live (unexpired)
                                                     delegation edges, oldest→newest.

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

_delegations: list[dict[str, Any]] = []
_DELEGATION_TTL_S: float = 30.0
_DELEGATION_MAX: int = 64


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


def record_delegation(task_id: str, from_id: str, to_id: str, label: str = "") -> None:
    """Record a real agent→agent delegation edge (e.g. Cerebro → specialist).

    Skips silently if from_id/to_id are empty or equal (no self-edges).
    Entries expire after _DELEGATION_TTL_S and the list is capped at
    _DELEGATION_MAX so this stays a short-lived stream, not a history log.
    Fail-soft: logs at DEBUG and swallows all exceptions.
    """
    try:
        if not from_id or not to_id or from_id == to_id:
            return
        now = datetime.now(tz=UTC)
        entry = {
            "task_id": task_id,
            "from": from_id,
            "to": to_id,
            "label": (label or "")[:120],
            "since": now.isoformat(),
            "_ts": now.timestamp(),
        }
        with _lock:
            _delegations.append(entry)
            cutoff = now.timestamp() - _DELEGATION_TTL_S
            _delegations[:] = [e for e in _delegations if e["_ts"] >= cutoff]
            del _delegations[:-_DELEGATION_MAX]
    except Exception:  # noqa: BLE001 — must never crash tool dispatch
        logger.debug(
            "hermes.live_activity.record_delegation_failed task_id=%s from=%s to=%s",
            task_id,
            from_id,
            to_id,
        )


def snapshot_delegations() -> list[dict[str, str]]:
    """Return a point-in-time copy of all live (unexpired) delegation edges.

    Each item: {"task_id", "from", "to", "label", "since"} (ISO-8601), oldest→
    newest insertion order. Also opportunistically prunes expired entries from
    the backing list. Returns an empty list on error.
    """
    try:
        with _lock:
            cutoff = datetime.now(tz=UTC).timestamp() - _DELEGATION_TTL_S
            live = [e for e in _delegations if e["_ts"] >= cutoff]
            _delegations[:] = live
            return [
                {
                    "task_id": e["task_id"],
                    "from": e["from"],
                    "to": e["to"],
                    "label": e["label"],
                    "since": e["since"],
                }
                for e in live
            ]
    except Exception:  # noqa: BLE001
        logger.debug("hermes.live_activity.snapshot_delegations_failed")
        return []
