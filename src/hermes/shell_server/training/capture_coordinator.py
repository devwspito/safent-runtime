"""TrainingCaptureCoordinator — F6.1: captura real durante modo training.

Actúa de pegamento entre:
  - ScreenCaptureService  → screenshots en PNG por step
  - MicAudioBackendPort   → WAVs del micrófono → WhisperWorker
  - TrainingSessionOrchestrator → capture_step con voz + screenshot

Diseño:
  - Inyectable: recibe interfaces, no concreciones. Los reales (Mutter,
    GstMic) se construyen en training/api.py; los fakes en tests.
  - Thread-safety: el coordinator guarda estado con _lock. Las capturas
    de audio corren en el thread del mic backend; cuando Whisper termina
    el caption se escribe en _state.voice_captions keyed by submission
    ordinal (not completion order) y se llama collected_voice_captions()
    para alimentar SkillCompiler.intent_caption en la ruta de firma.
  - FR-040 equivalente: el WAV se pasa al WhisperWorker que lo transcribe
    en su thread; el coordinator no conserva la ruta tras submit().
  - voice_opt_out: bool explícito requerido cuando mic_backend is None.
    None-sin-opt-out lanza VoiceRequiredError para evitar sesiones mudas
    en callers que no son el panel GTK4.

Ciclo de vida:
    coord.begin(session_id, voice_opt_out=False)   # mic activo
    coord.capture_screen_step(session_id, surface_kind, action_payload)
    ... múltiples veces ...
    coord.end(session_id)         # detiene mic + pantalla
    captions = coord.collected_voice_captions(session_id=session_id)
    # captions fed into compile_and_persist → SkillCompiler.intent_caption
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSessionOrchestrator,
)


class VoiceRequiredError(RuntimeError):
    """Raised when mic_backend is None but voice_opt_out was not set."""
from hermes.agents_os.application.whisper_worker import (
    TranscriptionJob,
    TranscriptionState,
    WhisperWorker,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.shell_server.screen_capture.domain import CaptureTarget, Frame
from hermes.shell_server.screen_capture.service import ScreenCaptureService
from hermes.shell_server.training.mic_audio_backend import MicAudioBackendPort
from hermes.shell_server.training.png_writer import encode_rgba_png

logger = logging.getLogger(__name__)

_TRAINING_BASE = Path("/var/lib/hermes/training")

# Identidad local del perfil personal-desktop (single-tenant en sesión). El
# perfil server multi-tenant usa el camino REST con identidades reales.
_LOCAL_TENANT = UUID("a9501e55-0000-4000-8000-000000000001")
_LOCAL_OPERATOR = UUID("a9501e55-0000-4000-8000-000000000002")


@dataclass
class _CaptureState:
    """Mutable in-flight state for one active capture session."""

    session_id: UUID
    session_dir: Path
    step_count: int = 0
    audio_jobs: list[TranscriptionJob] = field(default_factory=list)
    # voice captions collected after transcription completes
    voice_captions: dict[int, str] = field(default_factory=dict)


class TrainingCaptureCoordinator:
    """Ties ScreenCaptureService + MicAudioBackend + WhisperWorker together.

    All dependencies are injected so unit tests can pass fakes without
    any compositor, GStreamer, or Whisper model.

    Args:
        orchestrator:    TrainingSessionOrchestrator (shared with the API layer).
        screen_service:  ScreenCaptureService pre-configured with a backend.
        whisper_worker:  WhisperWorker pre-configured with a backend.
        mic_backend:     MicAudioBackendPort implementation.
        monitor_connector: PipeWire connector name (e.g. "Virtual-1").
        base_dir:        Root directory for session artifacts.
                         Defaults to /var/lib/hermes/training.
    """

    def __init__(
        self,
        *,
        orchestrator: TrainingSessionOrchestrator,
        screen_service: ScreenCaptureService,
        whisper_worker: WhisperWorker,
        mic_backend: MicAudioBackendPort | None,
        monitor_connector: str = "Virtual-1",
        base_dir: Path = _TRAINING_BASE,
    ) -> None:
        self._orchestrator = orchestrator
        self._screen = screen_service
        self._whisper = whisper_worker
        self._mic = mic_backend
        self._connector = monitor_connector
        self._base_dir = base_dir
        self._lock = threading.Lock()
        self._state: _CaptureState | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def begin(
        self,
        *,
        session_id: UUID,
        skill_name: str = "skill",
        voice_opt_out: bool = False,
    ) -> None:
        """Start screen + mic capture for *session_id* and the local session.

        Arranca el orquestador LOCAL (RECORDING) con voice_required derivado
        del micrófono — esta es la única fuente de verdad del flujo GTK4; el
        /start REST solo refleja el estado en la BD. Idempotente por sesión.

        Args:
            voice_opt_out: the human trainer explicitly chose to train without
                           microphone.  When False (default) and mic_backend is
                           None (no device found by probe_mic_backend), raises
                           VoiceRequiredError to surface the absence loudly
                           instead of silently producing a voice-less skill.
        """
        # mic_backend None means no device was detected by probe_mic_backend.
        # That is NOT an implicit opt-out — the user must call begin() with
        # voice_opt_out=True to acknowledge the absence, or the invariant
        # (never silently degrade to muted skill) is violated.
        if self._mic is None and not voice_opt_out:
            raise VoiceRequiredError(
                "No se detectó micrófono. Para entrenar sin voz pasa "
                "voice_opt_out=True de forma explícita."
            )

        with self._lock:
            if self._state is not None and self._state.session_id == session_id:
                return
            session_dir = self._base_dir / str(session_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            self._state = _CaptureState(
                session_id=session_id,
                session_dir=session_dir,
            )

        # voice_required: hubo micrófono real Y el usuario no hizo opt-out.
        # Con esto sign()/compile rechazan una skill muda (invariante de voz).
        voice_required = self._mic is not None and not voice_opt_out
        self._orchestrator.start(
            tenant_id=_LOCAL_TENANT,
            human_user_id=_LOCAL_OPERATOR,
            skill_id=skill_name,
            surface_kinds_allowed=frozenset(SurfaceKind),
            session_id=session_id,
            voice_required=voice_required,
        )

        target = CaptureTarget.monitor(self._connector)
        self._screen.start(target)
        self._whisper.start_background()

        if self._mic is not None:
            self._mic.start(
                session_dir=session_dir,
                on_chunk=self._on_audio_chunk,
            )
        logger.info(
            "capture_coordinator.begin session_id=%s voice_required=%s opt_out=%s",
            session_id,
            voice_required,
            voice_opt_out,
        )

    def capture_screen_step(
        self,
        *,
        session_id: UUID,
        surface_kind: SurfaceKind,
        action_payload: dict,
        language_hint: str = "es",
    ) -> int:
        """Grab a screenshot from the latest frame and record a step.

        Returns the step's sequence_index.

        The voice_caption is filled later by the mic/Whisper pipeline;
        the step is initially stored without it.
        """
        with self._lock:
            state = self._require_state(session_id)
            step_index = state.step_count

        screenshot_path = self._grab_screenshot(state, step_index)

        self._orchestrator.capture_step(
            session_id=session_id,
            surface_kind=surface_kind,
            action_payload=dict(action_payload),
            screenshot_path=str(screenshot_path) if screenshot_path else None,
        )

        with self._lock:
            state.step_count += 1

        logger.debug(
            "capture_coordinator.step surface=%s index=%s screenshot=%s",
            surface_kind,
            step_index,
            screenshot_path,
        )
        return step_index

    def end(self, *, session_id: UUID, wait_whisper_ms: float = 5000.0) -> None:
        """Stop mic + screen capture, drain pending Whisper, move to REVIEWING.

        Espera (acotado por wait_whisper_ms) a que las transcripciones en vuelo
        terminen, para que sign() vea la voz completa, y transiciona la sesión
        local a REVIEWING para que pueda firmarse/compilarse.
        """
        import time  # noqa: PLC0415

        with self._lock:
            self._require_state(session_id)

        if self._mic is not None:
            self._mic.stop()
        self._screen.stop()

        # Drena las transcripciones pendientes (acotado) para que la voz esté
        # completa antes de firmar (#28: wait_whisper_ms honrado).
        deadline = max(0.0, wait_whisper_ms / 1000.0)
        waited = 0.0
        while (
            self.pending_audio_count(session_id=session_id) > 0
            and waited < deadline
        ):
            time.sleep(0.1)
            waited += 0.1

        # Pasa a REVIEWING (la sesión local es la autoridad del flujo GTK4).
        try:
            self._orchestrator.request_review(session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            # Sin steps no se puede revisar; sign() lo rechazará después.
            logger.warning("request_review omitido: %s", exc)
        logger.info(
            "capture_coordinator.end session_id=%s waited=%.1fs", session_id, waited
        )

    def active_session_id(self) -> UUID | None:
        with self._lock:
            return self._state.session_id if self._state else None

    def pending_audio_count(self, *, session_id: UUID) -> int:
        """Number of audio chunks whose caption has not yet been written.

        Uses the orchestrator's pending_voice_chunks counter, which is
        decremented only after _wait_and_apply_caption completes — giving
        the true "all captions ready" signal.
        """
        try:
            sess = self._orchestrator.get_session(session_id=session_id)
            return sess.pending_voice_chunks
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require_state(self, session_id: UUID) -> _CaptureState:
        if self._state is None or self._state.session_id != session_id:
            raise RuntimeError(
                f"No active capture for session {session_id}. Call begin() first."
            )
        return self._state

    def _grab_screenshot(
        self, state: _CaptureState, step_index: int
    ) -> Path | None:
        frame: Frame | None = self._screen.latest_frame()
        if frame is None or frame.is_blank():
            logger.debug("capture_coordinator.screenshot blank or missing step=%s", step_index)
            return None

        png_path = state.session_dir / f"step_{step_index:04d}.png"
        try:
            png_bytes = encode_rgba_png(frame.width, frame.height, frame.data)
            png_path.write_bytes(png_bytes)
        except Exception:
            logger.exception("capture_coordinator.screenshot_write_failed path=%s", png_path)
            return None

        return png_path

    def _on_audio_chunk(self, wav_path: Path) -> None:
        """Called from the mic backend thread when a WAV chunk is ready."""
        with self._lock:
            if self._state is None:
                return
            session_id = self._state.session_id
            # Capture the submission ordinal inside the lock so that concurrent
            # completions key captions by submission order, not completion order.
            submission_ordinal = len(self._state.audio_jobs)

        self._orchestrator.increment_pending_voice_chunks(
            session_id=session_id, delta=1
        )
        job = self._whisper.submit(
            audio_path=wav_path,
            language_hint="es",
            training_session_id=session_id,
        )
        with self._lock:
            if self._state and self._state.session_id == session_id:
                self._state.audio_jobs.append(job)

        # Register a completion callback by watching the result in a thread.
        # This is deliberately simple: polling is fine given 10-s chunks.
        t = threading.Thread(
            target=self._wait_and_apply_caption,
            args=(session_id, job, submission_ordinal),
            daemon=True,
            name=f"whisper-wait-{job.job_id}",
        )
        t.start()

    def _wait_and_apply_caption(
        self, session_id: UUID, job: TranscriptionJob, ordinal: int
    ) -> None:
        """Block until the job is done, then store the caption and decrement pending.

        Captions are keyed by *submission* ordinal (captured at submit time
        inside _on_audio_chunk's lock), so the dict is ordered by chunk arrival
        regardless of which Whisper thread finishes first.

        Whisper FAILED results are tracked separately via failed_voice_chunks
        so the sign gate can surface them to the user rather than silently
        producing a voice-less skill.
        """
        import time  # noqa: PLC0415

        # Poll at 100 ms intervals (fast enough for 10-s chunks in production,
        # and instant for FakeWhisperBackend in tests).
        for _ in range(3000):  # up to 5 minutes
            result = self._whisper.get_result(job_id=job.job_id)
            if result.state in (
                TranscriptionState.DONE,
                TranscriptionState.FAILED,
                TranscriptionState.CANCELLED,
            ):
                break
            time.sleep(0.1)

        result = self._whisper.get_result(job_id=job.job_id)
        caption = result.text or ""

        if result.state == TranscriptionState.FAILED:
            logger.warning(
                "capture_coordinator.whisper_failed job=%s error=%s",
                job.job_id,
                result.error,
            )
            # Track as a failure so sign() can surface it — do not silently
            # absorb the failure into an empty caption.
            try:
                self._orchestrator.record_failed_voice_chunk(session_id=session_id)
            except Exception:
                logger.debug(
                    "capture_coordinator.failed_chunk_skipped session=%s", session_id
                )

        with self._lock:
            if self._state and self._state.session_id == session_id:
                # Key by submission ordinal, not by len(voice_captions), so
                # out-of-order completions do not scramble the caption sequence.
                self._state.voice_captions[ordinal] = caption

        try:
            self._orchestrator.increment_pending_voice_chunks(
                session_id=session_id, delta=-1
            )
        except Exception:
            # Session may have been abandoned — not an error.
            logger.debug(
                "capture_coordinator.pending_decrement_skipped session=%s", session_id
            )

    def collected_voice_captions(self, *, session_id: UUID) -> list[str]:
        """Return all completed voice captions for this session (for SkillCompiler)."""
        with self._lock:
            state = self._require_state(session_id)
            return [
                state.voice_captions[k]
                for k in sorted(state.voice_captions)
                if state.voice_captions[k]
            ]
