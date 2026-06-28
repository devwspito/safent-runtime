"""Regression: concurrent waiters on the SAME deterministic proposal_id must all
be signalled — a single-slot registry stranded all but the last waiter (cross-
conversation HITL interference)."""
from __future__ import annotations

import threading

import pytest

from hermes.runtime.security_hook import (
    _register_pending_event,
    _unregister_pending_event,
    signal_native_danger_approval,
)

pytestmark = pytest.mark.unit


def test_signal_wakes_all_concurrent_waiters():
    pid = "11111111-1111-1111-1111-111111111111"
    slots = [{"event": threading.Event(), "choice": None} for _ in range(3)]
    for s in slots:
        _register_pending_event(pid, s)
    try:
        fired = signal_native_danger_approval(pid, "approved")
        assert fired is True
        # EVERY concurrent waiter must be woken with the choice — not just one.
        for s in slots:
            assert s["event"].is_set() is True
            assert s["choice"] == "approved"
    finally:
        for s in slots:
            _unregister_pending_event(pid, s)


def test_unregister_only_removes_own_slot():
    pid = "22222222-2222-2222-2222-222222222222"
    a = {"event": threading.Event(), "choice": None}
    b = {"event": threading.Event(), "choice": None}
    _register_pending_event(pid, a)
    _register_pending_event(pid, b)
    _unregister_pending_event(pid, a)
    # b still registered → signal finds it
    assert signal_native_danger_approval(pid, "denied") is True
    assert b["choice"] == "denied" and not a["event"].is_set()
    _unregister_pending_event(pid, b)
    # now empty → signal finds nothing
    assert signal_native_danger_approval(pid, "approved") is False
