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

import contextvars
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


# Ambient per-thread active agent_id for THIS cycle (Enterprise Fase 2 Phase 1).
# Mirrors set/get/clear_current_cycle_task exactly: the security hook only
# receives (tool_name, args, task_id) from hermes-agent's plugin manager —
# never the active agent_id — so the native per-agent access-scope floor
# (security_hook._check_agent_access_scope) resolves it from here instead of
# threading a new kwarg through a plugin surface this repo does not own.
_current_agent = threading.local()


def set_current_cycle_agent(agent_id: str) -> None:
    """Stamp the current cycle's active agent_id for THIS thread (the cycle's executor thread)."""
    _current_agent.agent_id = agent_id or ""


def get_current_cycle_agent() -> str:
    """The current cycle's active agent_id for THIS thread, or "" outside a cycle."""
    return getattr(_current_agent, "agent_id", "")


def clear_current_cycle_agent() -> None:
    """Drop the thread's current-cycle agent stamp (cycle's finally; reused-thread safe)."""
    _current_agent.agent_id = ""


# Ambient current-turn user message for intent-based semantic tool retrieval.
# A ContextVar (NOT thread-local): the async cycle (run_cycle) resolves external
# tools in the EVENT-LOOP thread via `await _tools_source()`, but the rest of the
# cycle body runs in a run_in_executor thread. A thread-local set in one is invisible
# to the other. ContextVars propagate down the `await` chain within the SAME asyncio
# task (so _tools_source sees it) and are isolated across concurrent cycles (each
# run_cycle is its own task) — exactly the scope this needs.
_current_message: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hermes_current_message", default=""
)


def set_current_message(message: str) -> None:
    """Stamp the current turn's user message (intent-based tool retrieval)."""
    _current_message.set(message or "")


def get_current_message() -> str:
    """The current turn's user message for THIS cycle/task, or "" outside a cycle."""
    return _current_message.get()


def clear_current_message() -> None:
    """Reset the current-turn message stamp (cycle end)."""
    _current_message.set("")


def resolve_conversation(effective_task_id: str) -> str:
    """Conversation for a write proposal: by its own task_id, else the ambient cycle.

    Single resolution point so EVERY HITL write path (hook or broker) anchors to
    the same conversation the owner is looking at — including the sequential write
    wrapper that receives no task_id.
    """
    return get_conversation_for_task(effective_task_id) or get_conversation_for_task(
        get_current_cycle_task()
    )


# Process-global map: per-cycle Nous task_id -> the REAL WorkQueue work_item_id
# (a UUID, or "" when the cycle has none — e.g. a synthetic/legacy call site).
#
# BUG FIX (2026-07): the in-cycle WRITE dispatch (_dispatch_write_proposal /
# _dispatch_external_write) called broker.dispatch() WITHOUT a work_item_id, so
# register_pending always persisted UUID(int=0) for delegated/autonomous cycles
# (no chat conversation_id to fall back on). approve_action then read back
# work_item_id=0, treated it as "no queue task" (native-danger path), and never
# re-enqueued the work item — the task stayed stuck in pending_approval forever
# (the owner's approval had no effect: "caducó antes de aprobarla"). Mirrors the
# conversation_id registry above so the SAME task_id resolves BOTH.
_work_item_by_task: dict[str, str] = {}


def set_work_item_for_task(task_id: str, work_item_id: "Any") -> None:
    """Bind a cycle's task_id to its real WorkQueue work_item_id. No-op if either empty."""
    if not task_id or work_item_id is None:
        return
    with _lock:
        _work_item_by_task[task_id] = str(work_item_id)


def get_work_item_for_task(task_id: str) -> str:
    """Resolve the work_item_id for a cycle's task_id, or "" if unknown."""
    if not task_id:
        return ""
    with _lock:
        return _work_item_by_task.get(task_id, "")


def clear_work_item_for_task(task_id: str) -> None:
    """Drop the binding once the cycle ends (called from the engine's finally)."""
    if not task_id:
        return
    with _lock:
        _work_item_by_task.pop(task_id, None)


def resolve_work_item(effective_task_id: str) -> "UUID | None":
    """work_item_id for a write proposal: by its own task_id, else the ambient cycle.

    Mirrors resolve_conversation() so every in-cycle WRITE dispatch threads the
    REAL work_item_id into broker.dispatch() instead of leaving it None (which
    register_pending would otherwise persist as UUID(int=0) — see module docstring
    above). Returns None if no binding exists (fail-safe: broker treats None as
    "no queue task", same as before this fix for genuinely task-less calls).
    """
    from uuid import UUID as _UUID  # noqa: PLC0415

    raw = get_work_item_for_task(effective_task_id) or get_work_item_for_task(
        get_current_cycle_task()
    )
    if not raw:
        return None
    try:
        return _UUID(raw)
    except (ValueError, AttributeError):
        return None


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
