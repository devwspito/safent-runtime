"""SkillState — state machine de SkillPackage (FR-021/022/027-030)."""

from __future__ import annotations

from enum import StrEnum


class SkillState(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    AUTONOMOUS = "autonomous"
    PENDING_RECONFIRMATION = "pending_reconfirmation"
    ARCHIVED = "archived"


class SkillStateTransitionError(RuntimeError):
    """Transición de SkillState no permitida (constitución IV)."""


_ALLOWED: dict[SkillState, frozenset[SkillState]] = {
    SkillState.DRAFT: frozenset({SkillState.VALIDATED, SkillState.ARCHIVED}),
    SkillState.VALIDATED: frozenset({SkillState.AUTONOMOUS, SkillState.ARCHIVED}),
    SkillState.AUTONOMOUS: frozenset(
        {SkillState.PENDING_RECONFIRMATION, SkillState.ARCHIVED}
    ),
    SkillState.PENDING_RECONFIRMATION: frozenset(
        {SkillState.VALIDATED, SkillState.ARCHIVED}
    ),
    SkillState.ARCHIVED: frozenset(),
}


def assert_transition(current: SkillState, target: SkillState) -> None:
    """Falla con SkillStateTransitionError si la transición no está permitida."""
    if target not in _ALLOWED[current]:
        raise SkillStateTransitionError(
            f"Transición SkillState no permitida: {current} → {target}. "
            f"Permitidas desde {current}: {sorted(_ALLOWED[current])}"
        )


def is_terminal(state: SkillState) -> bool:
    return not _ALLOWED[state]


def is_eligible_for_autonomous_run(state: SkillState) -> bool:
    """FR-022: solo AUTONOMOUS puede ejecutarse."""
    return state == SkillState.AUTONOMOUS
