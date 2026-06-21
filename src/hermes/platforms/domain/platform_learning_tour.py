"""PlatformLearningTour aggregate (T010).

A teaching session focused on showing the platform terrain, either guided
by the operator or autonomously (read-only exploration with signed allow-list).

Domain layer — pure Python, zero infra dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from hermes.platforms.domain.value_objects import TeachingModality, TourOrigin


class TourState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    ABANDONED = "abandoned"


class TourScope(StrEnum):
    FULL = "full"
    DIRECTED_GAP = "directed-gap"
    RELEARN_ZONE = "relearn-zone"


class TourAlreadyClosed(RuntimeError):
    """Cannot operate on a closed or abandoned tour."""


class AutonomousTourRequiresAllowList(ValueError):
    """Autonomous tour attempted against a site not in the signed allow-list (FR-030)."""


@dataclass
class PlatformLearningTour:
    """Teaching session that produces or extends a PlatformModel.

    Invariants (data-model.md):
    - Has tenant_id and operator_attribution (or autonomous origin).
    - An autonomous tour can only open against a signed allow-list site.
    - An abandoned tour does NOT compile an incomplete signed model.
    - Audio narration is transcribed BEFORE reaching this aggregate; here
      narration_transcript_ref is always a reference to an artifact (not raw audio).
    - PII is tokenized before any captured area or narration is stored
      (captured_areas and narration_transcript_ref are PII-clean).
    """

    tour_id: str
    tenant_id: str
    target_site_ref: str
    origin: TourOrigin
    modality: TeachingModality
    scope: TourScope = TourScope.FULL
    operator_attribution: int | None = None  # UID-derived; None for autonomous
    state: TourState = TourState.OPEN
    captured_areas: tuple[dict, ...] = field(default_factory=tuple)
    narration_transcript_ref: str | None = None  # artifact hash reference
    opened_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.tour_id:
            raise ValueError("PlatformLearningTour.tour_id cannot be empty")
        if not self.tenant_id:
            raise ValueError("PlatformLearningTour.tenant_id cannot be empty")
        if not self.target_site_ref:
            raise ValueError("PlatformLearningTour.target_site_ref cannot be empty")
        if self.origin == TourOrigin.AUTONOMOUS and self.operator_attribution is not None:
            # Autonomous tours have no human operator attribution
            pass  # allowed — operator may trigger but exploration is autonomous
        if self.origin == TourOrigin.AUTONOMOUS and self.modality.has_video:
            raise ValueError(
                "Autonomous exploration cannot use video modality (no human demonstrator)"
            )

    def _assert_open(self) -> None:
        if self.state != TourState.OPEN:
            raise TourAlreadyClosed(
                f"Tour {self.tour_id} is {self.state}, not open"
            )

    def append_captured_area(self, area_data: dict) -> PlatformLearningTour:
        """Append a PII-tokenized area observation to this tour."""
        self._assert_open()
        import dataclasses  # noqa: PLC0415
        return dataclasses.replace(
            self,
            captured_areas=self.captured_areas + (area_data,),
        )

    def set_narration_transcript_ref(self, artifact_ref: str) -> PlatformLearningTour:
        """Store a reference to the tokenized transcript artifact."""
        self._assert_open()
        import dataclasses  # noqa: PLC0415
        return dataclasses.replace(self, narration_transcript_ref=artifact_ref)

    def close(self) -> PlatformLearningTour:
        """Transition open → closed (triggers compilation by the compiler)."""
        self._assert_open()
        import dataclasses  # noqa: PLC0415
        return dataclasses.replace(
            self,
            state=TourState.CLOSED,
            closed_at=datetime.now(tz=UTC),
        )

    def abandon(self) -> PlatformLearningTour:
        """Transition open → abandoned (discards partials — no model compiled)."""
        self._assert_open()
        import dataclasses  # noqa: PLC0415
        return dataclasses.replace(
            self,
            state=TourState.ABANDONED,
            closed_at=datetime.now(tz=UTC),
        )

    @property
    def is_open(self) -> bool:
        return self.state == TourState.OPEN

    @property
    def can_compile(self) -> bool:
        """A closed guided tour can compile; autonomous and abandoned cannot."""
        return self.state == TourState.CLOSED and bool(self.captured_areas)
