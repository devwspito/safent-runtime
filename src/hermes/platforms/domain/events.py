"""Domain events for the Platforms bounded context (T012).

All events are immutable frozen dataclasses. Payloads contain only
domain identifiers and metadata — never PII or raw selectors (SC-008).

Events go to the audit hash-chain with attribution (FR-035, NFR-006).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


def _now() -> datetime:
    return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Platform tour events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatformTourStarted:
    tour_id: str
    tenant_id: str
    target_site_ref: str
    origin: str
    operator_attribution: str | None
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class PlatformTourClosed:
    tour_id: str
    tenant_id: str
    platform_model_id: str
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class PlatformTourAbandoned:
    tour_id: str
    tenant_id: str
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


# ---------------------------------------------------------------------------
# PlatformModel lifecycle events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatformModelCompiled:
    platform_model_id: str
    version: int
    area_count: int
    entity_count: int
    rule_count: int
    origin: str
    signature_hex: str
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class PlatformModelConfirmed:
    platform_model_id: str
    version: int
    by_uid: int
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class PlatformModelEnabled:
    platform_model_id: str
    version: int
    by_uid: int
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class PlatformModelDisabled:
    platform_model_id: str
    version: int
    by_uid: int
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class PlatformModelDeprecated:
    platform_model_id: str
    by_uid: int
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class PlatformZoneMarkedStale:
    platform_model_id: str
    zone_id: str
    reason: str
    relearn_request_id: str
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class PlatformModelAmended:
    """Emitted when a model is amended (gap covered or zone re-learned)."""

    platform_model_id: str
    old_version: int
    new_version: int
    changed_zone_ids: tuple[str, ...]
    preserved_zone_hashes: tuple[str, ...]
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


# ---------------------------------------------------------------------------
# TaskOverModel events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskOverModelResolved:
    task_over_model_id: str
    platform_model_id: str
    model_version: int
    agent_id: str
    outcome: str
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class TaskBlockedByHouseRule:
    task_over_model_id: str
    rule_kind: str
    target_area_ref: str
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class TaskPausedByModelGap:
    task_over_model_id: str
    gap_id: str
    missing_descriptor: str
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class ModelGapCovered:
    gap_id: str
    platform_model_id: str
    new_version: int
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class ModelGapRejected:
    gap_id: str
    reason: str
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


# ---------------------------------------------------------------------------
# Capability binding events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityBoundToAgent:
    binding_id: str
    tenant_id: str
    agent_id: str
    capability_kind: str
    capability_id: str
    capability_version: str
    bound_by: int
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())


@dataclass(frozen=True, slots=True)
class CapabilityUnboundFromAgent:
    binding_id: str
    tenant_id: str
    agent_id: str
    capability_kind: str
    capability_id: str
    unbound_by: int
    occurred_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", self.occurred_at or _now())
