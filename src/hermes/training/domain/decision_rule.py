"""DecisionRule — regla declarativa derivada de narración categórica (FR-016/031/032).

Invariantes (constitución IV):
- confidence en [0, 1].
- source == NARRATED_TRAINING ⟹ categorical_markers no vacío.
- enforce_fail_closed: confidence < 0.85 ⟹ requires_review=True + risk_level=HIGH.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class DecisionRuleSource(StrEnum):
    NARRATED_TRAINING = "narrated_training"
    LLM_COMPILE_INFERRED = "llm_compile_inferred"
    SELF_HEALING = "self_healing"


_CONFIDENCE_THRESHOLD = 0.85


@dataclass(frozen=True, slots=True)
class DecisionRule:
    """Regla declarativa inmutable."""

    rule_id: UUID = field(default_factory=uuid4)
    training_session_id: UUID | None = None
    skill_package_id: UUID | None = None
    tenant_id: UUID | None = None
    step_id: UUID | None = None
    source: DecisionRuleSource = DecisionRuleSource.LLM_COMPILE_INFERRED
    pattern: dict[str, Any] = field(default_factory=dict)
    action: str = ""
    risk_level: RiskLevel = RiskLevel.HIGH
    confidence: float = 0.0
    requires_review: bool = True
    categorical_markers: tuple[str, ...] = ()
    inferred_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence debe estar en [0, 1], got {self.confidence}"
            )
        if (
            self.source == DecisionRuleSource.NARRATED_TRAINING
            and not self.categorical_markers
        ):
            raise ValueError(
                "NARRATED_TRAINING requiere al menos un categorical_marker"
            )


def enforce_fail_closed(rule: DecisionRule) -> DecisionRule:
    """Constitución IV: confidence < 0.85 ⟹ requires_review=True + risk_level=HIGH.

    Si la regla ya está en revisión, devuelve la misma instancia (idempotente).
    """
    if rule.requires_review:
        return rule
    if rule.confidence >= _CONFIDENCE_THRESHOLD:
        return rule
    return DecisionRule(
        rule_id=rule.rule_id,
        training_session_id=rule.training_session_id,
        skill_package_id=rule.skill_package_id,
        tenant_id=rule.tenant_id,
        step_id=rule.step_id,
        source=rule.source,
        pattern=rule.pattern,
        action=rule.action,
        risk_level=RiskLevel.HIGH,
        confidence=rule.confidence,
        requires_review=True,
        categorical_markers=rule.categorical_markers,
        inferred_at=rule.inferred_at,
    )
