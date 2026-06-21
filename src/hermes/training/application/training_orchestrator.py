"""TrainingOrchestrator — extiende StepRecorder para sesiones training (T092).

Coordina AudioCapturePort + TranscriptionPort + StepRecorder para popular
las columnas extendidas en modo training:
  - mode='training'
  - human_operator_id
  - audio_clip_ref (efímero, descartado tras transcripción — FR-040)
  - audio_transcript_*
  - mouse_track_blob
  - latency_warning

Estado interno por training_session_id. Sin romper la firma pública de
StepRecorder (FR-042).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from hermes.browser.application.step_recorder import StepRecord, StepRecorder

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TrainingStepMeta:
    """Metadatos de training para un step (columnas extendidas migración 0005)."""

    step_id: UUID
    training_session_id: UUID
    tenant_id: UUID
    human_operator_id: UUID
    mode: str = "training"
    audio_clip_ref: str | None = None
    audio_transcript: str | None = None
    audio_transcript_confidence: float | None = None
    mouse_track_blob: bytes | None = None
    latency_warning: bool = False


class TrainingStepMetaSink(Protocol):
    """Persiste el TrainingStepMeta junto al StepRecord base."""

    async def append_meta(self, meta: TrainingStepMeta) -> None: ...


@dataclass
class _SessionState:
    training_session_id: UUID
    tenant_id: UUID
    human_operator_id: UUID
    latency_warning: bool = False
    # step_id → mouse_track_blob en captura (descartado tras flush)
    pending_tracks: dict[UUID, bytes] = field(default_factory=dict)
    # step_id → transcript ya disponible (async desde Whisper)
    pending_transcripts: dict[UUID, tuple[str, float]] = field(default_factory=dict)


class TrainingOrchestrator:
    """Coordina el StepRecorder base con el tracking extra de training.

    Inyectable sobre cualquier StepRecorder de spec 001 sin tocar su firma.
    El meta-sink recibe el TrainingStepMeta una vez finalizado el step.

    NFR-001a: los timestamps vienen del StepRecorder interno (dentro de la VM).
    """

    def __init__(
        self,
        *,
        step_recorder: StepRecorder,
        meta_sink: TrainingStepMetaSink,
    ) -> None:
        self._recorder = step_recorder
        self._meta_sink = meta_sink
        self._sessions: dict[UUID, _SessionState] = {}

    def start_session(
        self,
        *,
        training_session_id: UUID,
        tenant_id: UUID,
        human_operator_id: UUID,
        latency_warning: bool = False,
    ) -> None:
        """Registra el contexto de la sesión de training."""
        self._sessions[training_session_id] = _SessionState(
            training_session_id=training_session_id,
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
            latency_warning=latency_warning,
        )
        logger.info(
            "training_session_started",
            extra={
                "training_session_id": str(training_session_id),
                "tenant_id": str(tenant_id),
                "latency_warning": latency_warning,
            },
        )

    def set_latency_warning(
        self, *, training_session_id: UUID, latency_warning: bool
    ) -> None:
        """Actualiza el flag de latencia para todos los steps futuros (T094)."""
        if training_session_id in self._sessions:
            self._sessions[training_session_id].latency_warning = latency_warning

    def attach_mouse_track(
        self,
        *,
        training_session_id: UUID,
        step_id: UUID,
        blob: bytes,
    ) -> None:
        """Asocia el blob de mouse track a un step pendiente (T093)."""
        state = self._sessions.get(training_session_id)
        if state is None:
            return
        state.pending_tracks[step_id] = blob

    def attach_transcript(
        self,
        *,
        training_session_id: UUID,
        step_id: UUID,
        transcript: str,
        confidence: float,
    ) -> None:
        """Asocia transcript de Whisper a un step pendiente (FR-010, FR-040).

        El audio_clip_ref ya fue descartado por el caller (FR-040).
        """
        state = self._sessions.get(training_session_id)
        if state is None:
            return
        state.pending_transcripts[step_id] = (transcript, confidence)

    async def flush_step(
        self,
        *,
        training_session_id: UUID,
        step_id: UUID,
        base_record: StepRecord,
    ) -> TrainingStepMeta:
        """Construye y persiste el TrainingStepMeta para el step.

        Llamar DESPUÉS de que el StepRecorder base haya cerrado el record.
        El audio_clip_ref es None (FR-040: descartado tras transcripción).
        """
        state = self._sessions.get(training_session_id)
        if state is None:
            raise RuntimeError(
                f"TrainingOrchestrator: sesión desconocida {training_session_id}"
            )

        transcript_entry = state.pending_transcripts.pop(step_id, None)
        audio_transcript = transcript_entry[0] if transcript_entry else None
        audio_transcript_confidence = transcript_entry[1] if transcript_entry else None

        mouse_track = state.pending_tracks.pop(step_id, None)

        meta = TrainingStepMeta(
            step_id=step_id,
            training_session_id=training_session_id,
            tenant_id=state.tenant_id,
            human_operator_id=state.human_operator_id,
            mode="training",
            audio_clip_ref=None,  # FR-040: nunca se persiste
            audio_transcript=audio_transcript,
            audio_transcript_confidence=audio_transcript_confidence,
            mouse_track_blob=mouse_track,
            latency_warning=state.latency_warning,
        )
        await self._meta_sink.append_meta(meta)
        return meta
