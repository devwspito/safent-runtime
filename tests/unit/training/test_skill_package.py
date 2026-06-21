"""Tests SkillPackage invariantes + can_be_signed."""
from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.training.domain.skill_package import SkillPackage
from hermes.training.domain.skill_state import SkillState

pytestmark = pytest.mark.unit


class TestInvariants:
    def test_skill_version_min_1(self) -> None:
        with pytest.raises(ValueError):
            SkillPackage(skill_version=0)

    def test_signature_hex_length_validated(self) -> None:
        with pytest.raises(ValueError):
            SkillPackage(signature_hex="deadbeef")
        SkillPackage(signature_hex="a" * 64)  # ok
        SkillPackage(signature_hex="")  # ok (sin firmar)


class TestCanBeSigned:
    def test_blocked_if_decision_rules_require_review(self) -> None:
        pkg = SkillPackage(
            state=SkillState.DRAFT, replay_script_id=uuid4()
        )
        assert not pkg.can_be_signed(decision_rules_requiring_review=1)

    def test_blocked_if_not_draft(self) -> None:
        pkg = SkillPackage(
            state=SkillState.VALIDATED, replay_script_id=uuid4()
        )
        assert not pkg.can_be_signed(decision_rules_requiring_review=0)

    def test_blocked_if_no_replay_script(self) -> None:
        pkg = SkillPackage(state=SkillState.DRAFT, replay_script_id=None)
        assert not pkg.can_be_signed(decision_rules_requiring_review=0)

    def test_can_sign_when_all_invariants_hold(self) -> None:
        pkg = SkillPackage(
            state=SkillState.DRAFT, replay_script_id=uuid4()
        )
        assert pkg.can_be_signed(decision_rules_requiring_review=0)
