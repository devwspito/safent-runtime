"""Unit tests for the delegation-edge registry in hermes.runtime.live_activity.

Verifies record_delegation / snapshot_delegations independently from the
existing record / snapshot (per-task tool) registry. Domain logic only —
no I/O, no HTTP, no DB, no clock faking.
"""

from __future__ import annotations

from hermes.runtime import live_activity


def _reset_all():
    live_activity._registry.clear()
    live_activity._delegations.clear()


class TestRecordDelegation:
    def setup_method(self):
        _reset_all()

    def teardown_method(self):
        _reset_all()

    def test_record_then_snapshot_returns_edge(self):
        live_activity.record_delegation(
            "t1", from_id="cerebro", to_id="roster-codigo-desarrollador", label="fix the bug"
        )
        snap = live_activity.snapshot_delegations()
        assert len(snap) == 1
        edge = snap[0]
        assert edge["task_id"] == "t1"
        assert edge["from"] == "cerebro"
        assert edge["to"] == "roster-codigo-desarrollador"
        assert edge["label"] == "fix the bug"
        assert "since" in edge
        assert "_ts" not in edge

    def test_snapshot_entry_has_exact_key_set(self):
        live_activity.record_delegation("t1", from_id="cerebro", to_id="roster-x", label="")
        edge = live_activity.snapshot_delegations()[0]
        assert set(edge.keys()) == {"task_id", "from", "to", "label", "since"}

    def test_self_edge_is_skipped(self):
        live_activity.record_delegation("t1", from_id="cerebro", to_id="cerebro", label="noop")
        assert live_activity.snapshot_delegations() == []

    def test_empty_from_id_is_skipped(self):
        live_activity.record_delegation("t1", from_id="", to_id="roster-x", label="")
        assert live_activity.snapshot_delegations() == []

    def test_empty_to_id_is_skipped(self):
        live_activity.record_delegation("t1", from_id="cerebro", to_id="", label="")
        assert live_activity.snapshot_delegations() == []

    def test_label_is_truncated_to_120_chars(self):
        long_label = "x" * 500
        live_activity.record_delegation(
            "t1", from_id="cerebro", to_id="roster-x", label=long_label
        )
        edge = live_activity.snapshot_delegations()[0]
        assert len(edge["label"]) == 120

    def test_missing_label_defaults_to_empty_string(self):
        live_activity.record_delegation("t1", from_id="cerebro", to_id="roster-x")
        edge = live_activity.snapshot_delegations()[0]
        assert edge["label"] == ""

    def test_record_delegation_fail_soft_on_bad_input(self):
        # Must not raise even with weird inputs.
        live_activity.record_delegation(None, from_id=None, to_id=None)  # type: ignore[arg-type]


class TestSnapshotDelegationsOrderingAndCap:
    def setup_method(self):
        _reset_all()

    def teardown_method(self):
        _reset_all()

    def test_two_distinct_edges_both_appear_in_insertion_order(self):
        live_activity.record_delegation("t1", from_id="cerebro", to_id="roster-a", label="first")
        live_activity.record_delegation("t2", from_id="cerebro", to_id="roster-b", label="second")
        snap = live_activity.snapshot_delegations()
        assert [e["to"] for e in snap] == ["roster-a", "roster-b"]

    def test_recording_beyond_cap_keeps_only_last_max(self):
        cap = live_activity._DELEGATION_MAX
        total = cap + 20
        for i in range(total):
            live_activity.record_delegation(
                f"t{i}", from_id="cerebro", to_id=f"roster-{i}", label=""
            )
        snap = live_activity.snapshot_delegations()
        assert len(snap) == cap
        # The cap keeps the MOST RECENT edges (oldest were evicted first).
        assert snap[-1]["to"] == f"roster-{total - 1}"
        assert snap[0]["to"] == f"roster-{total - cap}"

    def test_snapshot_empty_when_no_delegations(self):
        assert live_activity.snapshot_delegations() == []


class TestIndependenceFromActivityRegistry:
    def setup_method(self):
        _reset_all()

    def teardown_method(self):
        _reset_all()

    def test_recording_activity_does_not_appear_in_delegations(self):
        live_activity.record("t1", "agent-a", "tool_search")
        assert live_activity.snapshot_delegations() == []
        assert len(live_activity.snapshot()) == 1

    def test_recording_delegation_does_not_appear_in_activity(self):
        live_activity.record_delegation("t1", from_id="cerebro", to_id="roster-x", label="")
        assert live_activity.snapshot() == []
        assert len(live_activity.snapshot_delegations()) == 1
