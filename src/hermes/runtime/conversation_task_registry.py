"""Process-global map: per-cycle Nous task_id → chat conversation_id.

The security `pre_tool_call` hook is built ONCE at startup and only receives
Nous's per-cycle `task_id` — a RANDOM uuid minted in `_run_conversation_with_cdp`
(nous_engine), unrelated to the chat thread. To anchor a HITL approval card to
the conversation the owner is actually looking at, the engine registers the
cycle's real `conversation_id` here (keyed by that task_id) right before running
the agent; the hook resolves it back when it registers a pending approval.

Without this, the approval row is stored with the random task_id as its
`conversation_id`, so the in-chat widget — which filters by the active thread —
never matches it and the card NEVER renders (the "I never saw an approval card"
bug). Process-global + locked: the engine runs the cycle in an executor thread
and the hook fires within it; keys are unique per cycle so there is no contention.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_conv_by_task: dict[str, str] = {}

# Ambient per-thread current cycle task_id. The cycle and ALL its tool handlers
# run in the same executor thread, but Nous's sequential WRITE wrapper does not
# forward task_id to the handler (signature is (args) only) — so a broker-routed
# write (install_mcp/install_skill/install_app/memory/clarify) cannot resolve the
# conversation by its own argument and would fall to the non-resuming retry queue
# ("approve does nothing"). The cycle stamps its task_id here so any write path
# can recover the conversation. Thread-local + cleared in the cycle's finally so a
# reused executor thread never leaks a stale binding.
_current = threading.local()


def set_conversation_for_task(task_id: str, conversation_id: str) -> None:
    """Bind a cycle's task_id to its chat conversation_id. No-op if either empty."""
    if not task_id or not conversation_id:
        return
    with _lock:
        _conv_by_task[task_id] = conversation_id


def get_conversation_for_task(task_id: str) -> str:
    """Resolve the chat conversation_id for a cycle's task_id, or "" if unknown."""
    if not task_id:
        return ""
    with _lock:
        return _conv_by_task.get(task_id, "")


def clear_conversation_for_task(task_id: str) -> None:
    """Drop the binding once the cycle ends (called from the engine's finally)."""
    if not task_id:
        return
    with _lock:
        _conv_by_task.pop(task_id, None)


def set_current_cycle_task(task_id: str) -> None:
    """Stamp the current cycle's task_id for THIS thread (the cycle's executor thread)."""
    _current.task_id = task_id or ""


def get_current_cycle_task() -> str:
    """The current cycle's task_id for THIS thread, or "" outside a cycle."""
    return getattr(_current, "task_id", "")


def clear_current_cycle_task() -> None:
    """Drop the thread's current-cycle stamp (cycle's finally; reused-thread safe)."""
    _current.task_id = ""


def resolve_conversation(effective_task_id: str) -> str:
    """Conversation for a write proposal: by its own task_id, else the ambient cycle.

    Single resolution point so EVERY HITL write path (hook or broker) anchors to
    the same conversation the owner is looking at — including the sequential write
    wrapper that receives no task_id.
    """
    return get_conversation_for_task(effective_task_id) or get_conversation_for_task(
        get_current_cycle_task()
    )


# Per-cycle circuit breaker for the WRITE-PROPOSAL path. Nous's tool_loop_guardrails
# do not see broker-routed gated tools (install_mcp/skill_manage go through
# block-and-resume, bypassing the standard tool loop), so a tool that keeps failing
# there re-proposes forever — each retry a fresh HITL card ("retry-spam"). Count
# failures per (thread-cycle, tool) and hard-stop after N so the agent stops and
# reports honestly instead of thrashing. Thread-local + reset per cycle.
_failcounts = threading.local()


def bump_write_tool_failure(tool_name: str) -> int:
    """Record a failed write-tool call this cycle; return the new count."""
    counts = getattr(_failcounts, "counts", None)
    if counts is None:
        counts = {}
        _failcounts.counts = counts
    counts[tool_name] = counts.get(tool_name, 0) + 1
    return counts[tool_name]


def write_tool_failure_count(tool_name: str) -> int:
    """Failed write-tool calls of `tool_name` this cycle (0 outside a cycle)."""
    return getattr(_failcounts, "counts", {}).get(tool_name, 0) if getattr(_failcounts, "counts", None) else 0


def reset_write_tool_failures() -> None:
    """Clear the per-cycle write-tool failure counters (cycle start/finally)."""
    _failcounts.counts = {}
