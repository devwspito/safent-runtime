"""ModelGap, DirectedTeachingRequest, TaskOverModel entities (T011).

Domain layer — pure Python, zero infra dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


# ---------------------------------------------------------------------------
# ModelGap
# ---------------------------------------------------------------------------


class GapState(StrEnum):
    OPEN = "open"
    COVERED = "covered"
    REJECTED = "rejected"


class GapAlreadyResolved(RuntimeError):
    """Cannot operate on a gap that is already covered or rejected."""


@dataclass
class ModelGap:
    """Registry of an area/entity/action that a task needed but the model didn't cover.

    Invariants (data-model.md):
    - Registered against the TaskOverModel that found it.
    - While open, NO mutating steps execute to cover it (FR-020, SC-005).
    - Opens exactly one DirectedTeachingRequest.
    """

    gap_id: str
    platform_model_id: str
    task_ref: str
    missing_descriptor: str
    context: str
    teaching_request_id: str
    state: GapState = GapState.OPEN
    detected_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def __post_init__(self) -> None:
        if not self.gap_id:
            raise ValueError("ModelGap.gap_id cannot be empty")
        if not self.platform_model_id:
            raise ValueError("ModelGap.platform_model_id cannot be empty")
        if not self.missing_descriptor:
            raise ValueError("ModelGap.missing_descriptor cannot be empty")
        if not self.teaching_request_id:
            raise ValueError("ModelGap.teaching_request_id cannot be empty")

    def _assert_open(self) -> None:
        if self.state != GapState.OPEN:
            raise GapAlreadyResolved(f"Gap {self.gap_id} is already {self.state}")

    def cover(self) -> ModelGap:
        self._assert_open()
        import dataclasses  # noqa: PLC0415
        return dataclasses.replace(self, state=GapState.COVERED)

    def reject(self) -> ModelGap:
        self._assert_open()
        import dataclasses  # noqa: PLC0415
        return dataclasses.replace(self, state=GapState.REJECTED)


# ---------------------------------------------------------------------------
# DirectedTeachingRequest
# ---------------------------------------------------------------------------


class TeachingRequestState(StrEnum):
    OPEN = "open"
    FULFILLED = "fulfilled"
    REJECTED = "rejected"


@dataclass
class DirectedTeachingRequest:
    """Request for a mini-tour to cover a gap or re-learn a stale zone."""

    request_id: str
    platform_model_id: str
    reason: str  # "gap" | "stale_zone"
    target_zone_or_descriptor: str
    state: TeachingRequestState = TeachingRequestState.OPEN

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("DirectedTeachingRequest.request_id cannot be empty")
        if self.reason not in ("gap", "stale_zone"):
            raise ValueError(
                f"DirectedTeachingRequest.reason must be 'gap' or 'stale_zone', got {self.reason!r}"
            )

    def fulfill(self) -> DirectedTeachingRequest:
        import dataclasses  # noqa: PLC0415
        return dataclasses.replace(self, state=TeachingRequestState.FULFILLED)

    def reject(self) -> DirectedTeachingRequest:
        import dataclasses  # noqa: PLC0415
        return dataclasses.replace(self, state=TeachingRequestState.REJECTED)


# ---------------------------------------------------------------------------
# TaskOverModel
# ---------------------------------------------------------------------------


class TaskOverModelState(StrEnum):
    QUEUED = "queued"
    REASONING = "reasoning"
    AWAITING_HITL = "awaiting_hitl"
    COMPLETED = "completed"
    BLOCKED_BY_RULE = "blocked_by_rule"
    PAUSED_BY_GAP = "paused_by_gap"
    FAILED = "failed"


class TaskRequiresEnabledModel(RuntimeError):
    """TaskOverModel can only operate on a habilitada PlatformModel (FR-013)."""


@dataclass
class TaskOverModel:
    """Execution of a natural-language task resolved over a PlatformModel.

    Invariants (data-model.md):
    - Only operates on a habilitada PlatformModel.
    - HouseRules (global + agent overlay) applied as hard fail-closed constraints.
    - Steps with risk=HIGH go through the HITL gate (Principio II).
    - If an uncovered area is found → register ModelGap, do not improvise (FR-020).
    """

    task_over_model_id: str
    work_item_ref: str
    agent_id: str
    platform_model_id: str
    model_version: int
    injected_portion_ref: str | None
    audit_chain_ref: str | None
    state: TaskOverModelState = TaskOverModelState.QUEUED
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def __post_init__(self) -> None:
        if not self.task_over_model_id:
            raise ValueError("TaskOverModel.task_over_model_id cannot be empty")
        if not self.platform_model_id:
            raise ValueError("TaskOverModel.platform_model_id cannot be empty")
        if not self.agent_id:
            raise ValueError("TaskOverModel.agent_id cannot be empty")

    def transition_to(self, state: TaskOverModelState) -> TaskOverModel:
        import dataclasses  # noqa: PLC0415
        return dataclasses.replace(self, state=state)
