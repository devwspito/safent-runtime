"""Tests del state machine SkillState (T074, FR-021/022/027-030)."""
from __future__ import annotations

import pytest

from hermes.training.domain.skill_state import (
    SkillState,
    SkillStateTransitionError,
    assert_transition,
    is_eligible_for_autonomous_run,
    is_terminal,
)

pytestmark = pytest.mark.unit


class TestSkillStateTransitions:
    def test_draft_to_validated_ok(self) -> None:
        assert_transition(SkillState.DRAFT, SkillState.VALIDATED)

    def test_validated_to_autonomous_ok(self) -> None:
        assert_transition(SkillState.VALIDATED, SkillState.AUTONOMOUS)

    def test_autonomous_to_pending_reconfirmation_ok(self) -> None:
        assert_transition(SkillState.AUTONOMOUS, SkillState.PENDING_RECONFIRMATION)

    def test_pending_reconfirmation_back_to_validated_ok(self) -> None:
        assert_transition(SkillState.PENDING_RECONFIRMATION, SkillState.VALIDATED)

    def test_any_state_to_archived_ok(self) -> None:
        for src in (
            SkillState.DRAFT,
            SkillState.VALIDATED,
            SkillState.AUTONOMOUS,
            SkillState.PENDING_RECONFIRMATION,
        ):
            assert_transition(src, SkillState.ARCHIVED)

    def test_draft_to_autonomous_blocked(self) -> None:
        with pytest.raises(SkillStateTransitionError):
            assert_transition(SkillState.DRAFT, SkillState.AUTONOMOUS)

    def test_validated_to_pending_reconfirmation_blocked(self) -> None:
        with pytest.raises(SkillStateTransitionError):
            assert_transition(SkillState.VALIDATED, SkillState.PENDING_RECONFIRMATION)

    def test_archived_is_terminal(self) -> None:
        assert is_terminal(SkillState.ARCHIVED)
        with pytest.raises(SkillStateTransitionError):
            assert_transition(SkillState.ARCHIVED, SkillState.VALIDATED)


class TestEligibilityForRun:
    def test_only_autonomous_is_eligible(self) -> None:
        assert is_eligible_for_autonomous_run(SkillState.AUTONOMOUS)
        for state in (
            SkillState.DRAFT,
            SkillState.VALIDATED,
            SkillState.PENDING_RECONFIRMATION,
            SkillState.ARCHIVED,
        ):
            assert not is_eligible_for_autonomous_run(state), (
                f"FR-022 violado: {state} no debe ser elegible para run autónomo"
            )
