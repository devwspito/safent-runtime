"""Tests TrainingSession state machine + invariantes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hermes.training.domain.training_session import (
    TrainingSession,
    TrainingSessionState,
    TrainingSessionTransitionError,
    assert_training_transition,
)

pytestmark = pytest.mark.unit


class TestStateMachine:
    def test_started_to_operating_to_closing_to_compiled(self) -> None:
        assert_training_transition(TrainingSessionState.STARTED, TrainingSessionState.OPERATING)
        assert_training_transition(TrainingSessionState.OPERATING, TrainingSessionState.CLOSING)
        assert_training_transition(TrainingSessionState.CLOSING, TrainingSessionState.COMPILED)

    def test_compiled_is_terminal(self) -> None:
        with pytest.raises(TrainingSessionTransitionError):
            assert_training_transition(TrainingSessionState.COMPILED, TrainingSessionState.STARTED)

    def test_any_state_can_crash(self) -> None:
        for src in (
            TrainingSessionState.STARTED,
            TrainingSessionState.OPERATING,
            TrainingSessionState.CLOSING,
        ):
            assert_training_transition(src, TrainingSessionState.CRASHED)

    def test_crashed_is_terminal(self) -> None:
        with pytest.raises(TrainingSessionTransitionError):
            assert_training_transition(TrainingSessionState.CRASHED, TrainingSessionState.COMPILED)


class TestInvariants:
    def test_default_expires_at_4h(self) -> None:
        s = TrainingSession()
        assert s.expires_at is not None
        delta = s.expires_at - s.started_at
        assert delta == timedelta(hours=4)

    def test_step_count_negative_rejected(self) -> None:
        with pytest.raises(ValueError):
            TrainingSession(step_count=-1)

    def test_is_expired_after_ttl(self) -> None:
        past = datetime.now(tz=UTC) - timedelta(hours=5)
        s = TrainingSession(started_at=past)
        assert s.is_expired(now=datetime.now(tz=UTC))

    def test_with_state_returns_new_instance(self) -> None:
        s = TrainingSession()
        s2 = s.with_state(TrainingSessionState.OPERATING)
        assert s2 is not s
        assert s.state == TrainingSessionState.STARTED  # original sin cambio
        assert s2.state == TrainingSessionState.OPERATING

    def test_with_state_invalid_raises(self) -> None:
        s = TrainingSession()
        with pytest.raises(TrainingSessionTransitionError):
            s.with_state(TrainingSessionState.COMPILED)
