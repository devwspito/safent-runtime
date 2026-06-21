"""PlatformModel aggregate + child entities (T009).

Domain layer — pure Python, zero infra dependencies.

State machine (FR-012, FR-013, data-model.md):
  provisional → aprendida  (confirm; guided tours only start here)
  provisional → deprecada  (deprecate)
  aprendida   → habilitada (explicit enable; fail-closed on needs_label)
  aprendida   → deprecada
  habilitada  → stale      (zone landmark broken)
  habilitada  → deprecada
  stale       → habilitada (zone re-learned and re-validated)
  stale       → deprecada

FORBIDDEN: provisional → habilitada (NO direct path).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hermes.platforms.domain.value_objects import (
    ActionRef,
    DomainName,
    EntityRelationship,
    HouseRuleKind,
    LandmarkKind,
    LifecycleState,
    ModelVersion,
    NavigationPath,
    PlatformModelId,
    PlatformModelSignature,
    TourOrigin,
    ZoneHash,
)

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class InvalidLifecycleTransition(ValueError):
    """Attempted state transition not allowed by the machine."""


class ModelNotConfirmed(RuntimeError):
    """Cannot enable a model that has not been confirmed (aprendida)."""


class ModelHasUnlabeledAreas(ValueError):
    """Cannot enable a model with areas marked needs_label (FR-004)."""


class ModelNotEnabled(RuntimeError):
    """Operation requires the model to be in habilitada state (FR-013)."""


# ---------------------------------------------------------------------------
# Child entities (defined in this file — they live inside PlatformModel)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatformArea:
    """A navigable area of the platform (entity within PlatformModel).

    Invariants:
    - Has a non-empty NavigationPath.
    - Has a DomainName OR is marked needs_label=True.
    - Belongs to exactly one Zone (zone_id).
    """

    area_id: str
    navigation_path: NavigationPath
    zone_id: str
    domain_name: DomainName | None = None
    needs_label: bool = False
    available_actions: tuple[ActionRef, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.area_id:
            raise ValueError("PlatformArea.area_id cannot be empty")
        if self.domain_name is None and not self.needs_label:
            raise ValueError(
                "PlatformArea must have a DomainName or needs_label=True"
            )
        if not self.zone_id:
            raise ValueError("PlatformArea.zone_id cannot be empty")


@dataclass(frozen=True, slots=True)
class BusinessEntity:
    """A business concept managed by the platform (entity within PlatformModel).

    Invariants:
    - Non-empty domain name.
    - Relationships reference other BusinessEntities of the same model.
    """

    entity_id: str
    domain_name: DomainName
    relationships: tuple[EntityRelationship, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.entity_id:
            raise ValueError("BusinessEntity.entity_id cannot be empty")


@dataclass(frozen=True, slots=True)
class NavigationLandmark:
    """A stable reference to 'where something is' in the platform.

    Invariants:
    - Belongs to a Zone.
    - locator_ref is a reference to a signed selector (not a raw selector).
    """

    landmark_id: str
    kind: LandmarkKind
    locator_ref: str
    zone_id: str
    is_stale: bool = False

    def __post_init__(self) -> None:
        if not self.landmark_id:
            raise ValueError("NavigationLandmark.landmark_id cannot be empty")
        if not self.locator_ref:
            raise ValueError("NavigationLandmark.locator_ref cannot be empty")
        if not self.zone_id:
            raise ValueError("NavigationLandmark.zone_id cannot be empty")

    def mark_stale(self) -> NavigationLandmark:
        return NavigationLandmark(
            landmark_id=self.landmark_id,
            kind=self.kind,
            locator_ref=self.locator_ref,
            zone_id=self.zone_id,
            is_stale=True,
        )


@dataclass(frozen=True, slots=True)
class HouseRule:
    """Categorical operator restriction on platform interaction.

    Invariants:
    - Only categorical rules (never/always/required step) are promoted.
    - Non-categorical narrations go as requires_review, not HouseRule.
    - phrasing contains no PII.
    """

    rule_id: str
    kind: HouseRuleKind
    target_area_ref: str
    phrasing: str

    def __post_init__(self) -> None:
        if not self.rule_id:
            raise ValueError("HouseRule.rule_id cannot be empty")
        if not self.phrasing or not self.phrasing.strip():
            raise ValueError("HouseRule.phrasing cannot be empty")
        if not self.target_area_ref:
            raise ValueError("HouseRule.target_area_ref cannot be empty")


@dataclass(frozen=True, slots=True)
class Zone:
    """Grouping of model elements sharing a ZoneHash for granular invalidation.

    Invariants:
    - ZoneHash is deterministic over the zone's content.
    - Updating a zone's content changes its hash; unchanged zones keep theirs (FR-022).
    """

    zone_id: str
    zone_hash: ZoneHash
    member_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.zone_id:
            raise ValueError("Zone.zone_id cannot be empty")


@dataclass(frozen=True, slots=True)
class StalenessMark:
    """Mark on a Zone whose landmark no longer matches.

    Invariants:
    - References an existing Zone.
    - Co-exists with an open DirectedTeachingRequest for re-learning.
    """

    zone_id: str
    detected_at: datetime
    reason: str
    relearn_request_id: str

    def __post_init__(self) -> None:
        if not self.zone_id:
            raise ValueError("StalenessMark.zone_id cannot be empty")
        if not self.relearn_request_id:
            raise ValueError("StalenessMark.relearn_request_id cannot be empty")


# ---------------------------------------------------------------------------
# _ALLOWED_TRANSITIONS: fail-closed state machine definition
# ---------------------------------------------------------------------------

_ALLOWED_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.PROVISIONAL: frozenset({
        LifecycleState.APRENDIDA,
        LifecycleState.DEPRECADA,
    }),
    LifecycleState.APRENDIDA: frozenset({
        LifecycleState.HABILITADA,
        LifecycleState.DEPRECADA,
    }),
    # habilitada → aprendida (disable), stale, or deprecada
    LifecycleState.HABILITADA: frozenset({
        LifecycleState.APRENDIDA,
        LifecycleState.STALE,
        LifecycleState.DEPRECADA,
    }),
    LifecycleState.STALE: frozenset({
        LifecycleState.HABILITADA,
        LifecycleState.DEPRECADA,
    }),
    LifecycleState.DEPRECADA: frozenset(),
}


# ---------------------------------------------------------------------------
# PlatformModel — aggregate root
# ---------------------------------------------------------------------------


@dataclass
class PlatformModel:
    """Aggregate root: the agent's persistent, signed competence over a platform.

    Enforces all invariants from data-model.md. Mutations return new instances
    (value-object style for the mutable parts) or raise domain exceptions.

    Invariants (data-model.md):
    1. Exactly one PlatformModelSignature covering model identity + content.
    2. No PII values in any field (only domain names and structure).
    3. Every PlatformArea has a DomainName or needs_label=True.
    4. A model with needs_label areas CANNOT transition to habilitada.
    5. Every Zone has a ZoneHash; additive update preserves unaffected hashes.
    6. Belongs to exactly one tenant_id.
    7. Only habilitada serves productive tasks.
    """

    platform_model_id: PlatformModelId
    version: ModelVersion
    tenant_id: str
    site_ref: str
    lifecycle_state: LifecycleState
    origin: TourOrigin
    areas: tuple[PlatformArea, ...]
    entities: tuple[BusinessEntity, ...]
    landmarks: tuple[NavigationLandmark, ...]
    house_rules: tuple[HouseRule, ...]
    zones: tuple[Zone, ...]
    staleness_marks: tuple[StalenessMark, ...]
    signature: PlatformModelSignature | None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("PlatformModel.tenant_id cannot be empty")
        if not self.site_ref:
            raise ValueError("PlatformModel.site_ref cannot be empty")

    # ------------------------------------------------------------------
    # Invariant helpers
    # ------------------------------------------------------------------

    @property
    def has_unlabeled_areas(self) -> bool:
        return any(a.needs_label for a in self.areas)

    @property
    def is_enabled(self) -> bool:
        return self.lifecycle_state == LifecycleState.HABILITADA

    def zone_hashes(self) -> dict[str, str]:
        return {z.zone_id: z.zone_hash.hex_digest for z in self.zones}

    # ------------------------------------------------------------------
    # State machine transitions — all fail-closed
    # ------------------------------------------------------------------

    def _assert_transition(self, target: LifecycleState) -> None:
        allowed = _ALLOWED_TRANSITIONS.get(self.lifecycle_state, frozenset())
        if target not in allowed:
            raise InvalidLifecycleTransition(
                f"Cannot transition {self.lifecycle_state} → {target} "
                f"(allowed: {sorted(str(s) for s in allowed)})"
            )

    def confirm(self) -> PlatformModel:
        """provisional → aprendida (human confirmation, FR-011)."""
        self._assert_transition(LifecycleState.APRENDIDA)
        return self._with_state(LifecycleState.APRENDIDA)

    def enable(self) -> PlatformModel:
        """aprendida → habilitada (explicit human action, fail-closed on needs_label)."""
        self._assert_transition(LifecycleState.HABILITADA)
        if self.has_unlabeled_areas:
            raise ModelHasUnlabeledAreas(
                f"Cannot enable model {self.platform_model_id}: "
                f"some areas are still needs_label (FR-004/FR-013)"
            )
        return self._with_state(LifecycleState.HABILITADA)

    def disable(self) -> PlatformModel:
        """habilitada → aprendida (explicit disable by operator)."""
        if self.lifecycle_state != LifecycleState.HABILITADA:
            raise InvalidLifecycleTransition(
                f"disable() requires habilitada state, current: {self.lifecycle_state}"
            )
        self._assert_transition(LifecycleState.APRENDIDA)
        return self._with_state(LifecycleState.APRENDIDA)

    def mark_zone_stale(self, staleness_mark: StalenessMark) -> PlatformModel:
        """habilitada → stale (landmark broken in runtime)."""
        self._assert_transition(LifecycleState.STALE)
        new_marks = self.staleness_marks + (staleness_mark,)
        return self._with_state(LifecycleState.STALE, staleness_marks=new_marks)

    def restore_from_stale(self) -> PlatformModel:
        """stale → habilitada (all zones re-learned and re-validated)."""
        self._assert_transition(LifecycleState.HABILITADA)
        if self.has_unlabeled_areas:
            raise ModelHasUnlabeledAreas(
                "Cannot restore stale model: some areas still needs_label"
            )
        return self._with_state(LifecycleState.HABILITADA, staleness_marks=())

    def deprecate(self) -> PlatformModel:
        """Any state → deprecada (GDPR right-to-erasure cascade)."""
        self._assert_transition(LifecycleState.DEPRECADA)
        return self._with_state(LifecycleState.DEPRECADA)

    # ------------------------------------------------------------------
    # Content mutation (amend) — preserves unaffected zone hashes (FR-022)
    # ------------------------------------------------------------------

    def amend(
        self,
        *,
        new_areas: tuple[PlatformArea, ...] | None = None,
        new_entities: tuple[BusinessEntity, ...] | None = None,
        new_landmarks: tuple[NavigationLandmark, ...] | None = None,
        new_house_rules: tuple[HouseRule, ...] | None = None,
        new_zones: tuple[Zone, ...],
        new_signature: PlatformModelSignature,
    ) -> PlatformModel:
        """Return an amended model (covering a gap or re-learning a zone).

        The returned model has version + 1 and preserves zone hashes of
        zones not included in new_zones (FR-022).
        """
        import dataclasses  # noqa: PLC0415

        unchanged_zones = {
            z.zone_id: z for z in self.zones
            if not any(nz.zone_id == z.zone_id for nz in new_zones)
        }
        merged_zones = tuple(unchanged_zones.values()) + tuple(new_zones)

        return dataclasses.replace(
            self,
            version=self.version.next(),
            areas=new_areas if new_areas is not None else self.areas,
            entities=new_entities if new_entities is not None else self.entities,
            landmarks=new_landmarks if new_landmarks is not None else self.landmarks,
            house_rules=new_house_rules if new_house_rules is not None else self.house_rules,
            zones=merged_zones,
            signature=new_signature,
            updated_at=datetime.now(tz=UTC),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _with_state(
        self,
        state: LifecycleState,
        *,
        staleness_marks: tuple[StalenessMark, ...] | None = None,
    ) -> PlatformModel:
        import dataclasses  # noqa: PLC0415

        kwargs: dict = {"lifecycle_state": state, "updated_at": datetime.now(tz=UTC)}
        if staleness_marks is not None:
            kwargs["staleness_marks"] = staleness_marks
        return dataclasses.replace(self, **kwargs)

    # ------------------------------------------------------------------
    # Summary (for D-Bus read-only responses — no PII, no raw selectors)
    # ------------------------------------------------------------------

    def to_summary_dict(self) -> dict:
        return {
            "model_id": str(self.platform_model_id),
            "site_ref": self.site_ref,
            "version": self.version.number,
            "lifecycle_state": str(self.lifecycle_state),
            "area_count": len(self.areas),
            "entity_count": len(self.entities),
            "rule_count": len(self.house_rules),
            "has_stale_zone": any(m for m in self.staleness_marks),
            "origin": str(self.origin),
        }

    def to_detail_dict(self) -> dict:
        """Detailed summary (GET supervision — no PII, no raw selectors, SC-009)."""
        return {
            "model_id": str(self.platform_model_id),
            "site_ref": self.site_ref,
            "version": self.version.number,
            "lifecycle_state": str(self.lifecycle_state),
            "areas": [
                {
                    "area_id": a.area_id,
                    "domain_name": str(a.domain_name) if a.domain_name else None,
                    "needs_label": a.needs_label,
                    "zone_id": a.zone_id,
                }
                for a in self.areas
            ],
            "entities": [
                {
                    "entity_id": e.entity_id,
                    "domain_name": str(e.domain_name),
                }
                for e in self.entities
            ],
            "house_rules": [
                {
                    "rule_id": r.rule_id,
                    "kind": str(r.kind),
                    "target_area_ref": r.target_area_ref,
                    "phrasing": r.phrasing,
                }
                for r in self.house_rules
            ],
            "zones": [
                {
                    "zone_id": z.zone_id,
                    "is_stale": any(
                        m.zone_id == z.zone_id for m in self.staleness_marks
                    ),
                }
                for z in self.zones
            ],
        }
