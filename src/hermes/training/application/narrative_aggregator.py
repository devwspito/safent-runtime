"""NarrativeAggregator — agrega VoiceFragments por step en VoiceNarrative (T096).

Toma una lista de VoiceFragments ya asociados y las agrupa en un VoiceNarrative
por TrainingSession, computando la NarrativeCompleteness.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from hermes.training.domain.voice_narrative import (
    VoiceFragment,
    VoiceNarrative,
    compute_completeness,
)


class NarrativeAggregator:
    """Agrega VoiceFragments en un VoiceNarrative para una TrainingSession."""

    def aggregate(
        self,
        *,
        training_session_id: UUID,
        tenant_id: UUID,
        fragments: list[VoiceFragment],
        total_steps: int,
        mic_granted: bool,
    ) -> VoiceNarrative:
        """Construye VoiceNarrative y computa completeness.

        Args:
            training_session_id: ID de la sesión de training.
            tenant_id: tenant estricto (multi-tenant).
            fragments: lista de VoiceFragments ya asociados a steps.
            total_steps: número total de steps de la sesión.
            mic_granted: si el formador concedió el micrófono (FR-006).
        """
        frag_tuple = tuple(fragments)
        completeness = compute_completeness(
            frag_tuple,
            total_steps,
            mic_granted=mic_granted,
        )
        return VoiceNarrative(
            narrative_id=uuid4(),
            training_session_id=training_session_id,
            tenant_id=tenant_id,
            fragments=frag_tuple,
            total_steps_in_session=total_steps,
            completeness=completeness,
        )
