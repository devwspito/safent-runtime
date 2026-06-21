"""InSessionCoordinatorFactory — builds TrainingCaptureCoordinator for GTK4 shell.

The coordinator is only valid inside the graphical session (mutter + PipeWire).
This factory attempts to wire up real backends and degrades gracefully when
hardware (mic) or compositor (mutter) is unavailable — e.g. in a dev VM.

Contrato (CRÍTICO — el audio NO es opcional en training):
  - Si fallan los imports gi/Gst/mutter → coordinator None; UI "captura no disponible".
  - El audio del micrófono es OBLIGATORIO: es la explicación hablada que
    Whisper transcribe = la intención que Hermes aprende. Si NO hay micrófono
    real, NO se entrena en silencio: `audio_available=False` y el panel DEBE
    avisar de forma visible y bloquear la firma de una skill muda.
  - ScreenCaptureService usa MutterGstBackend (lazy import).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_TRAINING_BASE = Path("/var/lib/hermes/training")


def _has_real_audio_source() -> bool:
    """True solo si existe un device de audio source REAL (micrófono).

    No basta con que el plugin pipewiresrc exista (siempre está): usamos
    Gst.DeviceMonitor con filtro Audio/Source para detectar hardware real.
    En una VM sin tarjeta esto devuelve False → el training debe AVISAR.
    """
    try:
        import gi  # noqa: PLC0415

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # noqa: PLC0415

        Gst.init(None)
        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter("Audio/Source", None)
        monitor.start()
        devices = monitor.get_devices()
        monitor.stop()
        return bool(devices)
    except Exception as exc:  # noqa: BLE001
        logger.info("audio-source probe failed (%s)", exc)
        return False


def probe_mic_backend() -> tuple[object | None, bool]:
    """Devuelve (mic_backend, audio_available).

    Hay micrófono real → (GstMicAudioBackend, True).
    NO hay micrófono → (None, False): el panel DEBE avisar; sin la explicación
    hablada Hermes no puede aprender la intención. Jamás training en silencio.
    """
    if not _has_real_audio_source():
        logger.warning("sin micrófono real; el training requiere voz del formador")
        return None, False
    try:
        from hermes.shell_server.training.mic_audio_backend import (  # noqa: PLC0415
            GstMicAudioBackend,
        )

        return GstMicAudioBackend(), True
    except Exception as exc:  # noqa: BLE001
        logger.warning("mic backend import failed (%s)", exc)
        return None, False


def build_in_session_coordinator(
    *,
    orchestrator,  # TrainingSessionOrchestrator
    base_dir: Path = _TRAINING_BASE,
    monitor_connector: str = "Virtual-1",
) -> tuple[object | None, bool]:
    """Construye el TrainingCaptureCoordinator para uso en sesión.

    Devuelve (coordinator, audio_available):
      - coordinator None si faltan deps gráficas (gi/Gst/Mutter) → UI
        "captura no disponible".
      - audio_available False si no hay micrófono real → el panel DEBE
        avisar y NO permitir firmar una skill sin explicación hablada.

    El coordinator se devuelve sin arrancar; begin() inicia la captura.
    """
    try:
        from hermes.agents_os.application.whisper_worker import (  # noqa: PLC0415
            FakeWhisperBackend,
            WhisperWorker,
        )
        from hermes.agents_os.infrastructure.faster_whisper_backend import (  # noqa: PLC0415
            FasterWhisperBackend,
            is_available as whisper_available,
        )
        from hermes.shell_server.screen_capture.service import ScreenCaptureService  # noqa: PLC0415
        from hermes.shell_server.training.capture_coordinator import (  # noqa: PLC0415
            TrainingCaptureCoordinator,
        )
    except ImportError as exc:
        logger.warning("in_session_factory: missing dependency (%s); capture disabled", exc)
        return None, False

    # Screen capture: MutterGstBackend (lazy gi import — safe to construct here).
    try:
        from hermes.shell_server.screen_capture.service import MutterGstBackend  # noqa: PLC0415

        screen_svc = ScreenCaptureService(backend=MutterGstBackend())
    except Exception as exc:  # noqa: BLE001
        logger.warning("in_session_factory: screen backend unavailable (%s)", exc)
        return None, False

    # Whisper backend: prefer faster-whisper if installed, else fake (CI).
    if whisper_available():
        whisper_backend = FasterWhisperBackend()
    else:
        logger.info("in_session_factory: faster-whisper not installed; using FakeWhisperBackend")
        whisper_backend = FakeWhisperBackend()
    whisper_worker = WhisperWorker(backend=whisper_backend)

    # Mic backend: el audio (voz del formador) es OBLIGATORIO en training.
    mic_backend, audio_available = probe_mic_backend()

    coordinator = TrainingCaptureCoordinator(
        orchestrator=orchestrator,
        screen_service=screen_svc,
        whisper_worker=whisper_worker,
        mic_backend=mic_backend,
        monitor_connector=monitor_connector,
        base_dir=base_dir,
    )
    logger.info(
        "in_session_factory: coordinator built audio_available=%s whisper=%s",
        audio_available,
        type(whisper_backend).__name__,
    )
    # Marca para que el coordinator/panel sepan si pueden transcribir voz.
    if mic_backend is not None and hasattr(coordinator, "begin"):
        coordinator.audio_available = audio_available  # type: ignore[attr-defined]
    return coordinator, audio_available
