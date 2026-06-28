"""Unit tests for hermes.runtime.live_activity.

Verifies the process-wide registry: record, clear, snapshot, and
thread-safety invariants. Domain logic only — no I/O, no HTTP, no DB.
"""

from __future__ import annotations

import threading

import pytest

from hermes.runtime import live_activity


@pytest.fixture(autouse=True)
def _reset_registry():
    """Ensure each test starts with a clean registry."""
    live_activity._registry.clear()
    yield
    live_activity._registry.clear()


class TestRecord:
    def test_record_creates_entry(self):
        live_activity.record("t1", "agent-a", "tool_search")
        snap = live_activity.snapshot()
        assert len(snap) == 1
        assert snap[0]["agent_id"] == "agent-a"
        assert snap[0]["tool"] == "tool_search"
        assert "since" in snap[0]

    def test_record_overwrites_previous_tool(self):
        live_activity.record("t1", "agent-a", "tool_search")
        live_activity.record("t1", "agent-a", "mcp__ruflo__swarm_init")
        snap = live_activity.snapshot()
        assert len(snap) == 1
        assert snap[0]["tool"] == "mcp__ruflo__swarm_init"

    def test_record_multiple_tasks(self):
        live_activity.record("t1", "agent-a", "tool_search")
        live_activity.record("t2", "agent-b", "mcp__ruflo__swarm_init")
        snap = live_activity.snapshot()
        tools = {e["tool"] for e in snap}
        assert tools == {"tool_search", "mcp__ruflo__swarm_init"}

    def test_record_fail_soft_on_bad_input(self):
        # Must not raise even with weird inputs.
        live_activity.record(None, None, None)  # type: ignore[arg-type]


class TestClear:
    def test_clear_removes_entry(self):
        live_activity.record("t1", "agent-a", "tool_search")
        live_activity.clear("t1")
        assert live_activity.snapshot() == []

    def test_clear_unknown_task_is_noop(self):
        live_activity.record("t1", "agent-a", "tool_search")
        live_activity.clear("unknown-task")
        assert len(live_activity.snapshot()) == 1

    def test_clear_only_removes_target_task(self):
        live_activity.record("t1", "agent-a", "tool_search")
        live_activity.record("t2", "agent-b", "mcp__ruflo__swarm_init")
        live_activity.clear("t1")
        snap = live_activity.snapshot()
        assert len(snap) == 1
        assert snap[0]["agent_id"] == "agent-b"


class TestSnapshot:
    def test_snapshot_returns_copy(self):
        live_activity.record("t1", "agent-a", "tool_search")
        s1 = live_activity.snapshot()
        s1.clear()
        assert len(live_activity.snapshot()) == 1

    def test_snapshot_empty_when_no_entries(self):
        assert live_activity.snapshot() == []

    def test_snapshot_entry_shape(self):
        live_activity.record("t1", "agent-a", "mcp__ruflo__swarm_init")
        entry = live_activity.snapshot()[0]
        # snapshot now includes task_id so concurrent tasks/agents can be filtered
        # per conversation (the chat view filters by it).
        assert set(entry.keys()) == {"task_id", "agent_id", "tool", "since"}


class TestThreadSafety:
    """Concurrent writers must not corrupt the registry."""

    def test_concurrent_record_and_clear(self):
        errors: list[Exception] = []

        def writer():
            try:
                for i in range(200):
                    live_activity.record(f"t{i}", "agent-a", f"tool_{i}")
                    live_activity.clear(f"t{i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        # After all clears the registry should be empty or contain only
        # entries from races (no corruption — just a valid dict).
        snap = live_activity.snapshot()
        assert isinstance(snap, list)


class TestRufloSignal:
    """Verify the ruflo detection predicate used by runtime_status."""

    def test_ruflo_active_when_mcp_ruflo_tool_present(self):
        live_activity.record("t1", "agent-a", "mcp__ruflo__swarm_init")
        snap = live_activity.snapshot()
        ruflo_active = any(e["tool"].startswith("mcp__ruflo__") for e in snap)
        assert ruflo_active is True

    def test_ruflo_not_active_for_other_tools(self):
        live_activity.record("t1", "agent-a", "tool_search")
        snap = live_activity.snapshot()
        ruflo_active = any(e["tool"].startswith("mcp__ruflo__") for e in snap)
        assert ruflo_active is False

    def test_ruflo_not_active_when_empty(self):
        snap = live_activity.snapshot()
        ruflo_active = any(e["tool"].startswith("mcp__ruflo__") for e in snap)
        assert ruflo_active is False
