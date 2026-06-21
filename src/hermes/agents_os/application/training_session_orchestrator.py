"""TrainingSessionOrchestrator — US2 P2 (training mode).

Spec 003 FR-024..FR-038. El humano demuestra la tarea; Hermes captura
audio (Whisper local) + acciones (SurfaceAdapter) + screenshots y al
finalizar produce un `SkillPackage` firmado.

State machine:
  NOT_STARTED → RECORDING → PAUSED ↔ RECORDING → REVIEWING → SIGNED
                                                           → ABANDONED

Reglas:
  - Solo capturamos surfaces en allowlist (heredada del perfil).
  - Pausar es first-class — el humano puede interrumpir y reanudar
    sin perder pasos previos.
  - El skill solo se firma si:
      * el humano confirma explícitamente en REVIEWING (FR-031),
      * al menos un step capturado,
      * la transcripción Whisper completada para los chunks de audio.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from hermes.agents_os.domain.surface_kind import SurfaceKind


class TrainingSessionState(StrEnum):
    NOT_STARTED = "not_started"
    RECORDING = "recording"
    PAUSED = "paused"
    REVIEWING = "reviewing"
    SIGNED = "signed"
    ABANDONED = "abandoned"


class TrainingSessionError(RuntimeError):
    pass


class TrainingStateInvalid(TrainingSessionError):
    pass


class NoStepsCapturedError(TrainingSessionError):
    """FR-031: no se puede firmar una sesión sin steps."""


class HumanConfirmationMissing(TrainingSessionError):
    """FR-031: firma requiere confirmación humana explícita."""


class VoiceCaptureRequired(TrainingSessionError):
    """Invariant: mic was active but no voice transcript was captured."""


@dataclass(frozen=True, slots=True)
class TrainingStep:
    """Step capturado durante la sesión."""

    step_id: UUID
    sequence_index: int
    surface_kind: SurfaceKind
    action_payload: dict
    captured_at: datetime
    voice_caption: str | None  # transcripción del audio durante el step
    screenshot_path: str | None


@dataclass(slots=True)
class TrainingSession:
    session_id: UUID
    tenant_id: UUID
    human_user_id: UUID
    skill_id: str
    state: TrainingSessionState
    started_at: datetime
    last_updated_at: datetime
    paused_at: datetime | None = None
    reviewed_at: datetime | None = None
    signed_at: datetime | None = None
    abandoned_at: datetime | None = None
    surface_kinds_allowed: frozenset[SurfaceKind] = field(
        default_factory=frozenset
    )
    steps: list[TrainingStep] = field(default_factory=list)
    pending_voice_chunks: int = 0
    # True when the session was started with an active mic and the user did NOT
    # explicitly opt out.  sign() enforces a positive voice transcript.
    voice_required: bool = False
    # Count of Whisper chunks that finished in FAILED state (not just empty).
    failed_voice_chunks: int = 0


class TrainingSessionOrchestrator:
    """Orquesta una colección de TrainingSessions activas."""

    def __init__(self, *, clock=lambda: datetime.now(tz=UTC)) -> None:
        self._sessions: dict[UUID, TrainingSession] = {}
        self._clock = clock
        # Protects pending_voice_chunks and failed_voice_chunks mutations which
        # are driven concurrently from Whisper completion threads.
        self._chunks_lock = threading.Lock()

    def start(
        self,
        *,
        tenant_id: UUID,
        human_user_id: UUID,
        skill_id: str,
        surface_kinds_allowed: frozenset[SurfaceKind],
        session_id: UUID | None = None,
        voice_required: bool = False,
    ) -> TrainingSession:
        now = self._clock()
        sid = session_id if session_id is not None else uuid4()
        sess = TrainingSession(
            session_id=sid,
            tenant_id=tenant_id,
            human_user_id=human_user_id,
            skill_id=skill_id,
            state=TrainingSessionState.RECORDING,
            started_at=now,
            last_updated_at=now,
            surface_kinds_allowed=surface_kinds_allowed,
            voice_required=voice_required,
        )
        self._sessions[sid] = sess
        return sess

    def capture_step(
        self,
        *,
        session_id: UUID,
        surface_kind: SurfaceKind,
        action_payload: dict,
        voice_caption: str | None = None,
        screenshot_path: str | None = None,
    ) -> TrainingStep:
        sess = self._fetch(session_id)
        if sess.state != TrainingSessionState.RECORDING:
            raise TrainingStateInvalid(
                f"capture requiere RECORDING, está {sess.state}"
            )
        if surface_kind not in sess.surface_kinds_allowed:
            raise PermissionError(
                f"surface_kind {surface_kind} fuera de allowlist"
            )
        step = TrainingStep(
            step_id=uuid4(),
            sequence_index=len(sess.steps),
            surface_kind=surface_kind,
            action_payload=dict(action_payload),
            captured_at=self._clock(),
            voice_caption=voice_caption,
            screenshot_path=screenshot_path,
        )
        sess.steps.append(step)
        sess.last_updated_at = self._clock()
        return step

    def pause(self, *, session_id: UUID) -> TrainingSession:
        sess = self._fetch(session_id)
        if sess.state != TrainingSessionState.RECORDING:
            raise TrainingStateInvalid(
                f"pause requiere RECORDING, está {sess.state}"
            )
        sess.state = TrainingSessionState.PAUSED
        sess.paused_at = self._clock()
        sess.last_updated_at = sess.paused_at
        return sess

    def resume(self, *, session_id: UUID) -> TrainingSession:
        sess = self._fetch(session_id)
        if sess.state != TrainingSessionState.PAUSED:
            raise TrainingStateInvalid(
                f"resume requiere PAUSED, está {sess.state}"
            )
        sess.state = TrainingSessionState.RECORDING
        sess.last_updated_at = self._clock()
        return sess

    def request_review(self, *, session_id: UUID) -> TrainingSession:
        sess = self._fetch(session_id)
        if sess.state not in (
            TrainingSessionState.RECORDING,
            TrainingSessionState.PAUSED,
        ):
            raise TrainingStateInvalid(
                f"review requiere RECORDING/PAUSED, está {sess.state}"
            )
        if not sess.steps:
            raise NoStepsCapturedError(
                "FR-031: no se puede revisar sesión sin steps"
            )
        sess.state = TrainingSessionState.REVIEWING
        sess.reviewed_at = self._clock()
        sess.last_updated_at = sess.reviewed_at
        return sess

    def sign(
        self,
        *,
        session_id: UUID,
        human_confirmed: bool,
        aggregated_caption: str = "",
        transcription_failed_ack: bool = False,
    ) -> TrainingSession:
        """Sign a session.

        Args:
            aggregated_caption:      combined transcript text from the coordinator.
                                     Required (non-empty) when voice_required is True,
                                     unless transcription_failed_ack is True.
            transcription_failed_ack: user explicitly acknowledged that Whisper failed
                                      on all chunks.  Allows signing despite empty
                                      transcript (still produces warning).
        """
        sess = self._fetch(session_id)
        if sess.state != TrainingSessionState.REVIEWING:
            raise TrainingStateInvalid(
                f"sign requiere REVIEWING, está {sess.state}"
            )
        if not human_confirmed:
            raise HumanConfirmationMissing(
                "FR-031: firma requiere confirmación humana explícita"
            )
        with self._chunks_lock:
            pending = sess.pending_voice_chunks
        if pending > 0:
            raise TrainingStateInvalid(
                f"hay {pending} chunks de voz pendientes"
            )
        # Positive voice invariant: if the session required voice (mic was
        # active and user did not opt-out), the aggregated transcript must not
        # be empty unless the user explicitly acknowledged that transcription
        # failed on every chunk.
        if sess.voice_required and not aggregated_caption.strip():
            if not transcription_failed_ack:
                raise VoiceCaptureRequired(
                    "Esta sesión requería voz pero no se capturó ninguna "
                    "transcripción. Conecta un micrófono y vuelve a entrenar, "
                    "o confirma que la transcripción falló para firmar de todos modos."
                )
        sess.state = TrainingSessionState.SIGNED
        sess.signed_at = self._clock()
        sess.last_updated_at = sess.signed_at
        return sess

    def abandon(self, *, session_id: UUID, reason: str) -> TrainingSession:
        sess = self._fetch(session_id)
        if sess.state == TrainingSessionState.SIGNED:
            raise TrainingStateInvalid(
                "no se puede abandonar una sesión SIGNED"
            )
        sess.state = TrainingSessionState.ABANDONED
        sess.abandoned_at = self._clock()
        sess.last_updated_at = sess.abandoned_at
        return sess

    def increment_pending_voice_chunks(
        self, *, session_id: UUID, delta: int = 1
    ) -> TrainingSession:
        sess = self._fetch(session_id)
        with self._chunks_lock:
            sess.pending_voice_chunks = max(0, sess.pending_voice_chunks + delta)
        return sess

    def record_failed_voice_chunk(self, *, session_id: UUID) -> TrainingSession:
        """Increment the failed_voice_chunks counter (Whisper returned FAILED)."""
        sess = self._fetch(session_id)
        with self._chunks_lock:
            sess.failed_voice_chunks += 1
        return sess

    def get_session(self, *, session_id: UUID) -> TrainingSession:
        return self._fetch(session_id)

    def _fetch(self, sid: UUID) -> TrainingSession:
        if sid not in self._sessions:
            raise TrainingStateInvalid(f"unknown session {sid}")
        return self._sessions[sid]
