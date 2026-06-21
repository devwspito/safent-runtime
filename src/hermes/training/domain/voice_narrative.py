"""VoiceNarrative y VoiceFragment (data-model §6, FR-009/010/040).

- ``VoiceFragment``: una unidad atómica de transcript producida por Whisper
  sobre un chunk de audio. Asociada a UN ``StepRecord`` vía
  ``transcript_associator`` (asignación asimétrica -8s/+4s).
- ``VoiceNarrative``: agregado por ``TrainingSession``. Contiene la lista
  ordenada de fragments + la metadata de completeness.

Invariantes:
- Cada fragment tiene ``confidence in [0, 1]``.
- El raw audio NUNCA persiste (FR-040). Solo el ``transcript`` queda.
- Edición manual del transcript (FR-018a) genera una nueva versión inmutable
  del fragment y deja el original como ``superseded_by`` (lineage).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from hermes.training.domain.narrative_completeness import NarrativeCompleteness


class VoiceFragmentState(StrEnum):
    RECORDING = "recording"  # audio en captura, no transcribed aún
    TRANSCRIBING = "transcribing"  # audio pasó a Whisper
    ASSOCIATED = "associated"  # transcript asignado a un StepRecord
    LOW_CONFIDENCE = "low_confidence"  # Whisper devolvió baja confianza
    EDITED = "edited"  # formador editó el transcript (FR-018a)
    SUPERSEDED = "superseded"  # versión previa de un fragment editado
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class VoiceFragment:
    """Transcript atómico asociado a un StepRecord.

    El ``confidence`` viene de Whisper. Por debajo de ``low_confidence_threshold``
    (default 0.6), el fragment NO se usa para inferir DecisionRules sin
    revisión humana (FR-031, edge case "ruido alto").
    """

    fragment_id: UUID = field(default_factory=uuid4)
    narrative_id: UUID | None = None
    step_id: UUID | None = None
    tenant_id: UUID | None = None
    transcript: str = ""
    transcript_language: str = "es-ES"
    confidence: float = 0.0
    state: VoiceFragmentState = VoiceFragmentState.RECORDING
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    superseded_by: UUID | None = None
    edited_by_operator_id: UUID | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence debe estar en [0, 1], got {self.confidence}"
            )

    def is_usable_for_rule_inference(self, *, threshold: float = 0.6) -> bool:
        """True si el fragment pasa el corte de confianza Y está ASSOCIATED."""
        if self.state not in (VoiceFragmentState.ASSOCIATED, VoiceFragmentState.EDITED):
            return False
        return self.confidence >= threshold


@dataclass(frozen=True, slots=True)
class VoiceNarrative:
    """Agregado de fragments por TrainingSession.

    Computa ``narrative_completeness`` a partir de la cobertura de steps:
    - 0 fragments OR mic_denied → NONE
    - todos los steps con un fragment válido → FULL
    - parcial → PARTIAL
    """

    narrative_id: UUID = field(default_factory=uuid4)
    training_session_id: UUID | None = None
    tenant_id: UUID | None = None
    fragments: tuple[VoiceFragment, ...] = ()
    total_steps_in_session: int = 0
    completeness: NarrativeCompleteness = NarrativeCompleteness.NONE
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


def compute_completeness(
    fragments: tuple[VoiceFragment, ...],
    total_steps: int,
    *,
    mic_granted: bool,
) -> NarrativeCompleteness:
    """Calcula NarrativeCompleteness a partir de los fragments.

    Reglas:
    - mic NO concedido → NONE
    - total_steps == 0 → NONE (defensa)
    - Sin fragments usables → NONE
    - Todos los steps con al menos 1 fragment ASSOCIATED+confidence ≥ 0.6 → FULL
    - Resto → PARTIAL
    """
    if not mic_granted or total_steps <= 0:
        return NarrativeCompleteness.NONE

    usable = [f for f in fragments if f.is_usable_for_rule_inference()]
    if not usable:
        return NarrativeCompleteness.NONE

    steps_covered = {f.step_id for f in usable if f.step_id is not None}
    if len(steps_covered) >= total_steps:
        return NarrativeCompleteness.FULL
    return NarrativeCompleteness.PARTIAL
