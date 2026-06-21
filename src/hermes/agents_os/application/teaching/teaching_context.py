"""TeachingContext and InputOwner — value objects for spec 004 / US3.

Rules encoded here:
- A TeachingContext is ALWAYS owned by OPERATOR (FR-018/FR-003).
- InputOwner.transfer_to is prohibited in teach mode: ownership is fixed
  to OPERATOR for the lifetime of the teaching session.
- Two contexts with the same isolation_key collide (FR-004).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class InputOwnershipViolation(RuntimeError):
    """Raised when input ownership invariant would be broken (FR-002/FR-022)."""


class SurfaceKind(StrEnum):
    """Teaching-surface kinds understood by the isolation layer.

    Mirrors only the surface kinds relevant to teaching context isolation;
    the full SurfaceKind taxonomy lives in agents_os.domain.surface_kind.
    """

    BROWSER = "browser"
    DESKTOP_APP = "desktop_app"


class InputOwner(StrEnum):
    """Who holds the input channel of a context (constitución: poseedor único)."""

    AGENT = "agent"
    OPERATOR = "operator"

    def transfer_to(self, target: InputOwner, *, in_teach_mode: bool) -> None:  # noqa: ARG002
        """Teaching mode prohibits ownership transfer — owner is always OPERATOR.

        Outside teaching mode this would allow agent↔operator swaps.
        For US3 the operator is the sole owner; no transfer is ever valid.

        Raises:
            InputOwnershipViolation: always when in_teach_mode is True.
        """
        if in_teach_mode:
            raise InputOwnershipViolation(
                "InputOwner.transfer_to is prohibited during teaching mode "
                "(FR-018): the operator holds sole input ownership for the "
                "lifetime of the teaching session."
            )


@dataclass(frozen=True, slots=True)
class TeachingContext:
    """Value object representing one isolated teaching context (FR-003).

    Invariants:
    - owner is always OPERATOR (set by factory, never mutated).
    - isolation_key is derived from (tenant_id, site_id) and must not
      overlap with any active execution context key.
    """

    context_id: UUID
    surface_kind: SurfaceKind
    isolation_key: str
    owner: InputOwner
    tenant_id: UUID
    site_id: str

    def __post_init__(self) -> None:
        if self.owner != InputOwner.OPERATOR:
            raise InputOwnershipViolation(
                f"TeachingContext.owner must be OPERATOR, got {self.owner!r}. "
                "Teaching contexts are always operator-owned (FR-018)."
            )

    def conflicts_with(self, other: TeachingContext) -> bool:
        """True when two contexts share the same isolation_key (FR-004)."""
        return self.isolation_key == other.isolation_key

    def storage_lock_key(self) -> str:
        """Lock key for serializing StorageState access (tenant_id, site_id).

        Used to serialize competing access when Teach and Execution sessions
        target the same (tenant_id, site_id) pair (FR-004).
        """
        return f"{self.tenant_id}:{self.site_id}"
