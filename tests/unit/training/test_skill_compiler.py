"""Tests del SkillCompiler (T099, FR-013/017).

Bloqueo si alguna regla tiene requires_review=True.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.training.application.skill_compiler import SkillCompilationError, SkillCompiler
from hermes.training.domain.decision_rule import DecisionRule, DecisionRuleSource, RiskLevel
from hermes.training.domain.narrative_completeness import NarrativeCompleteness
from hermes.training.domain.skill_state import SkillState
from hermes.training.domain.training_session import TrainingSession, TrainingSessionState
from hermes.training.domain.voice_narrative import VoiceNarrative

pytestmark = pytest.mark.unit


def _session() -> TrainingSession:
    return TrainingSession(
        training_session_id=uuid4(),
        workspace_id=uuid4(),
        tenant_id=uuid4(),
        human_operator_id=uuid4(),
        state=TrainingSessionState.COMPILED,
    )


def _narrative() -> VoiceNarrative:
    return VoiceNarrative(
        narrative_id=uuid4(),
        training_session_id=uuid4(),
        tenant_id=uuid4(),
        completeness=NarrativeCompleteness.FULL,
    )


def _rule(requires_review: bool = False, risk: RiskLevel = RiskLevel.LOW) -> DecisionRule:
    return DecisionRule(
        source=DecisionRuleSource.LLM_COMPILE_INFERRED,
        risk_level=risk,
        requires_review=requires_review,
        confidence=0.95 if not requires_review else 0.7,
        categorical_markers=("siempre",) if requires_review else (),
    )


class TestSkillCompilerBlockedByReview:
    def test_blocked_if_any_rule_requires_review(self) -> None:
        compiler = SkillCompiler(runtime_version="0.0.1-test")
        session = _session()
        narrative = _narrative()
        rules = [_rule(requires_review=False), _rule(requires_review=True)]

        with pytest.raises(SkillCompilationError) as exc_info:
            compiler.compile(
                session=session,
                replay_script_id=uuid4(),
                narrative=narrative,
                decision_rules=rules,
            )
        assert "FR-017" in str(exc_info.value)
        assert "requires_review=True" in str(exc_info.value)

    def test_blocked_if_multiple_rules_require_review(self) -> None:
        compiler = SkillCompiler()
        session = _session()
        narrative = _narrative()
        rules = [_rule(requires_review=True), _rule(requires_review=True)]

        with pytest.raises(SkillCompilationError):
            compiler.compile(
                session=session,
                replay_script_id=uuid4(),
                narrative=narrative,
                decision_rules=rules,
            )


class TestSkillCompilerHappyPath:
    def test_compiles_draft_package(self) -> None:
        compiler = SkillCompiler(runtime_version="0.0.1-test")
        session = _session()
        narrative = _narrative()
        replay_script_id = uuid4()
        rules = [_rule(), _rule()]

        pkg = compiler.compile(
            session=session,
            replay_script_id=replay_script_id,
            narrative=narrative,
            decision_rules=rules,
        )

        assert pkg.state == SkillState.DRAFT
        assert pkg.replay_script_id == replay_script_id
        assert pkg.voice_narrative_id == narrative.narrative_id
        assert len(pkg.decision_rule_ids) == 2
        assert pkg.tenant_id == session.tenant_id
        assert pkg.compiled_by_operator_id == session.human_operator_id
        assert pkg.signature_hex == ""  # sin firmar todavía

    def test_empty_rules_compiles_ok(self) -> None:
        compiler = SkillCompiler()
        session = _session()
        narrative = _narrative()
        pkg = compiler.compile(
            session=session,
            replay_script_id=uuid4(),
            narrative=narrative,
            decision_rules=[],
        )
        assert pkg.state == SkillState.DRAFT
        assert len(pkg.decision_rule_ids) == 0

    def test_runtime_version_is_set(self) -> None:
        compiler = SkillCompiler(runtime_version="1.2.3")
        session = _session()
        pkg = compiler.compile(
            session=session,
            replay_script_id=uuid4(),
            narrative=_narrative(),
            decision_rules=[],
        )
        assert pkg.runtime_version == "1.2.3"
