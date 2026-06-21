"""Tests de DecisionRule + enforce_fail_closed (T074, FR-016/031/032)."""
from __future__ import annotations

import pytest

from hermes.training.domain.decision_rule import (
    DecisionRule,
    DecisionRuleSource,
    RiskLevel,
    enforce_fail_closed,
)

pytestmark = pytest.mark.unit


class TestInvariants:
    def test_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            DecisionRule(confidence=1.5)
        with pytest.raises(ValueError):
            DecisionRule(confidence=-0.1)

    def test_narrated_training_requires_markers(self) -> None:
        with pytest.raises(ValueError):
            DecisionRule(
                source=DecisionRuleSource.NARRATED_TRAINING,
                categorical_markers=(),
            )

    def test_llm_inferred_does_not_require_markers(self) -> None:
        r = DecisionRule(
            source=DecisionRuleSource.LLM_COMPILE_INFERRED,
            categorical_markers=(),
        )
        assert r.categorical_markers == ()


class TestFailClosedConstitution:
    def test_low_confidence_forces_review_and_high(self) -> None:
        r = DecisionRule(
            source=DecisionRuleSource.LLM_COMPILE_INFERRED,
            risk_level=RiskLevel.LOW,
            requires_review=False,
            confidence=0.7,  # bajo el umbral 0.85
        )
        enforced = enforce_fail_closed(r)
        assert enforced.requires_review is True
        assert enforced.risk_level == RiskLevel.HIGH

    def test_high_confidence_keeps_original(self) -> None:
        r = DecisionRule(
            source=DecisionRuleSource.LLM_COMPILE_INFERRED,
            risk_level=RiskLevel.LOW,
            requires_review=False,
            confidence=0.95,
        )
        enforced = enforce_fail_closed(r)
        assert enforced is r  # mismo objeto (sin cambio)

    def test_already_requires_review_passthrough(self) -> None:
        r = DecisionRule(
            source=DecisionRuleSource.LLM_COMPILE_INFERRED,
            risk_level=RiskLevel.MEDIUM,
            requires_review=True,
            confidence=0.5,
        )
        enforced = enforce_fail_closed(r)
        # Ya está en review, no se modifica
        assert enforced is r
