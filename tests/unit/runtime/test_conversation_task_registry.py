"""Unit tests for conversation_task_registry.

Regression coverage for the "HITL approval card never shows in chat" fix: the
security hook and the broker external-write path resolve the REAL chat
conversation_id for a cycle from this process-global registry (keyed by the
random per-cycle task_id). If set/get/clear regress, approvals get the wrong
conversation_id and the in-chat card silently disappears again.
"""

from __future__ import annotations

from hermes.runtime.conversation_task_registry import (
    clear_conversation_for_task,
    get_conversation_for_task,
    set_conversation_for_task,
)


def test_set_get_roundtrip() -> None:
    set_conversation_for_task("task-1", "conv-1")
    assert get_conversation_for_task("task-1") == "conv-1"
    clear_conversation_for_task("task-1")


def test_clear_removes_binding() -> None:
    set_conversation_for_task("task-2", "conv-2")
    clear_conversation_for_task("task-2")
    # Post-clear miss is "" (NOT the stale conv) so the broker stores NULL, not a
    # wrong thread — a leaked binding would anchor the next cycle's card wrongly.
    assert get_conversation_for_task("task-2") == ""


def test_unknown_task_returns_empty() -> None:
    assert get_conversation_for_task("never-registered") == ""


def test_empty_args_are_noops() -> None:
    # Non-chat cycles pass conversation_id="" — must NOT create a bogus binding.
    set_conversation_for_task("task-3", "")
    assert get_conversation_for_task("task-3") == ""
    set_conversation_for_task("", "conv-x")
    assert get_conversation_for_task("") == ""


def test_overwrite_keeps_latest() -> None:
    set_conversation_for_task("task-4", "conv-a")
    set_conversation_for_task("task-4", "conv-b")
    assert get_conversation_for_task("task-4") == "conv-b"
    clear_conversation_for_task("task-4")


def test_clear_unknown_is_safe() -> None:
    clear_conversation_for_task("nope")  # must not raise
    assert get_conversation_for_task("nope") == ""
