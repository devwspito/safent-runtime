"""Tests TrainingSessionOrchestrator (FR-024..FR-038 US2)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.agents_os.application.training_session_orchestrator import (
    HumanConfirmationMissing,
    NoStepsCapturedError,
    TrainingSessionOrchestrator,
    TrainingSessionState,
    TrainingStateInvalid,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

pytestmark = pytest.mark.unit


@pytest.fixture
def orch() -> TrainingSessionOrchestrator:
    return TrainingSessionOrchestrator()


def _start_session(orch):
    return orch.start(
        tenant_id=uuid4(),
        human_user_id=uuid4(),
        skill_id="invoice-upload",
        surface_kinds_allowed=frozenset(
            {SurfaceKind.BROWSER, SurfaceKind.DESKTOP_APP}
        ),
    )


class TestStart:
    def test_start_returns_recording(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = _start_session(orch)
        assert s.state == TrainingSessionState.RECORDING
        assert s.steps == []


class TestCapture:
    def test_capture_appends_step(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = _start_session(orch)
        step = orch.capture_step(
            session_id=s.session_id,
            surface_kind=SurfaceKind.BROWSER,
            action_payload={"click": "#submit"},
            voice_caption="ahora pulso submit",
        )
        assert step.sequence_index == 0
        assert step.voice_caption == "ahora pulso submit"

    def test_capture_outside_allowlist_blocked(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = _start_session(orch)
        with pytest.raises(PermissionError):
            orch.capture_step(
                session_id=s.session_id,
                surface_kind=SurfaceKind.TERMINAL,
                action_payload={},
            )

    def test_capture_when_paused_blocked(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = _start_session(orch)
        orch.pause(session_id=s.session_id)
        with pytest.raises(TrainingStateInvalid):
            orch.capture_step(
                session_id=s.session_id,
                surface_kind=SurfaceKind.BROWSER,
                action_payload={},
            )


class TestPauseResume:
    def test_pause_then_resume(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = _start_session(orch)
        s = orch.pause(session_id=s.session_id)
        assert s.state == TrainingSessionState.PAUSED
        s = orch.resume(session_id=s.session_id)
        assert s.state == TrainingSessionState.RECORDING

    def test_resume_when_recording_raises(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = _start_session(orch)
        with pytest.raises(TrainingStateInvalid):
            orch.resume(session_id=s.session_id)


class TestReview:
    def test_review_requires_steps(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = _start_session(orch)
        with pytest.raises(NoStepsCapturedError):
            orch.request_review(session_id=s.session_id)

    def test_review_transitions(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = _start_session(orch)
        orch.capture_step(
            session_id=s.session_id,
            surface_kind=SurfaceKind.BROWSER,
            action_payload={"x": 1},
        )
        s = orch.request_review(session_id=s.session_id)
        assert s.state == TrainingSessionState.REVIEWING


class TestSign:
    def _full_flow(self, orch):
        s = _start_session(orch)
        orch.capture_step(
            session_id=s.session_id,
            surface_kind=SurfaceKind.BROWSER,
            action_payload={"x": 1},
        )
        orch.request_review(session_id=s.session_id)
        return s

    def test_sign_requires_human_confirmation(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = self._full_flow(orch)
        with pytest.raises(HumanConfirmationMissing):
            orch.sign(session_id=s.session_id, human_confirmed=False)

    def test_sign_blocked_if_voice_chunks_pending(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = self._full_flow(orch)
        orch.increment_pending_voice_chunks(
            session_id=s.session_id, delta=1
        )
        with pytest.raises(TrainingStateInvalid):
            orch.sign(session_id=s.session_id, human_confirmed=True)

    def test_sign_happy(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = self._full_flow(orch)
        s = orch.sign(session_id=s.session_id, human_confirmed=True)
        assert s.state == TrainingSessionState.SIGNED
        assert s.signed_at is not None


class TestAbandon:
    def test_abandon_anywhere_except_signed(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = _start_session(orch)
        s = orch.abandon(session_id=s.session_id, reason="user_cancel")
        assert s.state == TrainingSessionState.ABANDONED
        assert s.abandoned_at is not None

    def test_abandon_signed_blocked(
        self, orch: TrainingSessionOrchestrator
    ) -> None:
        s = _start_session(orch)
        orch.capture_step(
            session_id=s.session_id,
            surface_kind=SurfaceKind.BROWSER,
            action_payload={"x": 1},
        )
        orch.request_review(session_id=s.session_id)
        orch.sign(session_id=s.session_id, human_confirmed=True)
        with pytest.raises(TrainingStateInvalid):
            orch.abandon(session_id=s.session_id, reason="x")
