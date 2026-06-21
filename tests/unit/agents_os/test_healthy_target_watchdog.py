"""Tests HealthyTargetWatchdog (FR-008 + FR-050 BLOQUEANTE)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.application.healthy_target_watchdog import (
    HealthyTargetWatchdog,
    WatchdogState,
    WatchdogStateInvalid,
)

pytestmark = pytest.mark.unit


class _FakeRollback:
    def __init__(self) -> None:
        self.called: list[tuple[UUID, str]] = []

    def rollback(self, *, attempt_id, reason) -> None:
        self.called.append((attempt_id, reason))


@pytest.fixture
def fake_rb() -> _FakeRollback:
    return _FakeRollback()


@pytest.fixture
def clock_t() -> dict:
    return {"now": datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)}


@pytest.fixture
def watchdog(fake_rb, clock_t) -> HealthyTargetWatchdog:
    return HealthyTargetWatchdog(
        rollback_port=fake_rb,
        clock=lambda: clock_t["now"],
        default_timeout_seconds=600,
    )


class TestBegin:
    def test_begin_creates_waiting_watcher(
        self, watchdog: HealthyTargetWatchdog
    ) -> None:
        aid = uuid4()
        w = watchdog.begin_watching(
            attempt_id=aid, target_image_version="v1.0.1"
        )
        assert w.state == WatchdogState.WAITING
        assert w.target_image_version == "v1.0.1"
        assert watchdog.get_watcher(attempt_id=aid).attempt_id == aid

    def test_begin_duplicate_blocked(
        self, watchdog: HealthyTargetWatchdog
    ) -> None:
        aid = uuid4()
        watchdog.begin_watching(
            attempt_id=aid, target_image_version="v1.0.1"
        )
        with pytest.raises(WatchdogStateInvalid):
            watchdog.begin_watching(
                attempt_id=aid, target_image_version="v1.0.1"
            )


class TestTargetReached:
    def test_mark_reached_transitions(
        self, watchdog: HealthyTargetWatchdog, fake_rb: _FakeRollback
    ) -> None:
        aid = uuid4()
        watchdog.begin_watching(
            attempt_id=aid, target_image_version="v1.0.1"
        )
        w = watchdog.mark_target_reached(attempt_id=aid)
        assert w.state == WatchdogState.TARGET_REACHED
        assert w.target_reached_at is not None
        # No rollback disparado.
        assert fake_rb.called == []

    def test_mark_reached_after_terminal_raises(
        self, watchdog: HealthyTargetWatchdog
    ) -> None:
        aid = uuid4()
        watchdog.begin_watching(
            attempt_id=aid, target_image_version="v1.0.1"
        )
        watchdog.mark_target_reached(attempt_id=aid)
        with pytest.raises(WatchdogStateInvalid):
            watchdog.mark_target_reached(attempt_id=aid)


class TestTimeout:
    def test_check_timeouts_triggers_rollback(
        self,
        watchdog: HealthyTargetWatchdog,
        fake_rb: _FakeRollback,
        clock_t: dict,
    ) -> None:
        aid = uuid4()
        watchdog.begin_watching(
            attempt_id=aid, target_image_version="v1.0.1"
        )
        # Avanzamos 11 minutos.
        clock_t["now"] = clock_t["now"] + timedelta(minutes=11)
        triggered = watchdog.check_timeouts()
        assert len(triggered) == 1
        assert triggered[0].state == WatchdogState.TIMEOUT
        assert fake_rb.called == [(aid, "healthy_target_timeout")]

    def test_check_timeouts_before_deadline_noop(
        self,
        watchdog: HealthyTargetWatchdog,
        fake_rb: _FakeRollback,
        clock_t: dict,
    ) -> None:
        aid = uuid4()
        watchdog.begin_watching(
            attempt_id=aid, target_image_version="v1.0.1"
        )
        clock_t["now"] = clock_t["now"] + timedelta(minutes=5)
        triggered = watchdog.check_timeouts()
        assert triggered == []
        assert fake_rb.called == []

    def test_check_timeouts_skips_terminal_watchers(
        self,
        watchdog: HealthyTargetWatchdog,
        fake_rb: _FakeRollback,
        clock_t: dict,
    ) -> None:
        aid = uuid4()
        watchdog.begin_watching(
            attempt_id=aid, target_image_version="v1.0.1"
        )
        watchdog.mark_target_reached(attempt_id=aid)
        clock_t["now"] = clock_t["now"] + timedelta(hours=2)
        triggered = watchdog.check_timeouts()
        assert triggered == []
        assert fake_rb.called == []


class TestAbort:
    def test_abort_transitions_to_aborted(
        self, watchdog: HealthyTargetWatchdog
    ) -> None:
        aid = uuid4()
        watchdog.begin_watching(
            attempt_id=aid, target_image_version="v1.0.1"
        )
        w = watchdog.abort_watching(
            attempt_id=aid, reason="manual_cancel"
        )
        assert w.state == WatchdogState.ABORTED

    def test_abort_idempotent_on_terminal(
        self, watchdog: HealthyTargetWatchdog
    ) -> None:
        aid = uuid4()
        watchdog.begin_watching(
            attempt_id=aid, target_image_version="v1.0.1"
        )
        watchdog.mark_target_reached(attempt_id=aid)
        w = watchdog.abort_watching(attempt_id=aid, reason="x")
        assert w.state == WatchdogState.TARGET_REACHED  # no overwrite
