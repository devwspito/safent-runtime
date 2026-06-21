"""GstMicAudioBackend — captura de audio desde PipeWire/micrófono vía GStreamer.

Produce archivos WAV temporales en /var/lib/hermes/training/<session_id>/
que el WhisperWorker consume.  El archivo se borra tras la transcripción.

Interfaz:
    MicAudioBackendPort   — protocolo abstracto (inyectable en tests).
    GstMicAudioBackend    — implementación real (lazy-import de Gst).
    FakeMicAudioBackend   — implementación determinista para CI.

El import de Gst se hace SIEMPRE dentro del método start() para no
impedir la importación del módulo en entornos sin GStreamer.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable
from uuid import UUID

logger = logging.getLogger(__name__)

# Callback invocado por el backend cuando un chunk WAV está listo en disco.
AudioChunkCallback = Callable[[Path], None]


@runtime_checkable
class MicAudioBackendPort(Protocol):
    """Contrato mínimo del backend de audio para el coordinador."""

    def start(
        self,
        session_dir: Path,
        on_chunk: AudioChunkCallback,
    ) -> None:
        """Empieza a capturar audio del micrófono.

        Args:
            session_dir: directorio donde guardar los WAVs temporales.
            on_chunk:    callback invocado con la ruta de cada WAV listo.
        """
        ...

    def stop(self) -> None:
        """Detiene la captura y libera recursos."""
        ...


class GstMicAudioBackend:
    """Captura audio desde el micrófono vía PipeWire → GStreamer → WAV.

    Pipeline: pipewiresrc ! audioconvert ! audioresample ! wavenc ! filesink

    Un chunk se produce cada `chunk_seconds` segundos de audio.  El
    backend divide automáticamente el stream en archivos WAV sucesivos
    rotando el filesink.

    Import de Gst es lazy — el módulo es importable en CI sin GStreamer.
    """

    _CHUNK_SECONDS = 10  # duración de cada chunk WAV

    def __init__(self, *, chunk_seconds: int = _CHUNK_SECONDS) -> None:
        self._chunk_seconds = chunk_seconds
        self._pipeline = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self, session_dir: Path, on_chunk: AudioChunkCallback) -> None:
        self._stop_event.clear()

        def _run() -> None:
            try:
                self._capture_loop(session_dir, on_chunk)
            except Exception:
                logger.exception("GstMicAudioBackend._capture_loop raised")

        self._thread = threading.Thread(
            target=_run, name="mic-audio-capture", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._pipeline is not None:
            try:
                # Lazy import mirrors _capture_loop; keeps module importable
                # in CI environments without GStreamer.
                import gi  # noqa: PLC0415

                gi.require_version("Gst", "1.0")
                from gi.repository import Gst  # noqa: PLC0415

                self._pipeline.set_state(Gst.State.NULL)
            except Exception:
                logger.warning(
                    "GstMicAudioBackend.stop: pipeline set_state(NULL) failed",
                    exc_info=True,
                )
        if self._thread is not None:
            # Join with a timeout larger than _CHUNK_SECONDS so the capture
            # loop has time to finish the current chunk's GLib timer.
            self._thread.join(timeout=self._chunk_seconds + 2.0)
            self._thread = None

    def _capture_loop(self, session_dir: Path, on_chunk: AudioChunkCallback) -> None:
        import gi  # noqa: PLC0415

        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst  # noqa: PLC0415

        Gst.init(None)
        chunk_idx = 0

        while not self._stop_event.is_set():
            wav_path = session_dir / f"audio_chunk_{chunk_idx:04d}.wav"
            pipeline_str = (
                f"pipewiresrc ! audioconvert ! audioresample ! "
                f"audio/x-raw,rate=16000,channels=1 ! wavenc ! "
                f"filesink location={wav_path}"
            )
            pipeline = Gst.parse_launch(pipeline_str)
            self._pipeline = pipeline
            pipeline.set_state(Gst.State.PLAYING)

            loop = GLib.MainLoop()
            bus = pipeline.get_bus()
            bus.add_signal_watch()

            deadline = self._chunk_seconds

            def _on_message(bus, message):
                if message.type == Gst.MessageType.ERROR:
                    loop.quit()

            bus.connect("message", _on_message)

            timer = GLib.timeout_add_seconds(
                deadline, lambda: loop.quit() or False
            )
            try:
                loop.run()
            finally:
                GLib.source_remove(timer)

            pipeline.set_state(Gst.State.NULL)
            self._pipeline = None

            if wav_path.exists() and wav_path.stat().st_size > 44:
                on_chunk(wav_path)
            chunk_idx += 1

            if self._stop_event.is_set():
                break


class FakeMicAudioBackend:
    """Backend de audio determinista para CI — no requiere PipeWire."""

    def __init__(
        self,
        *,
        chunks_to_emit: int = 1,
        transcript_text: str = "acción de prueba",
    ) -> None:
        self._chunks_to_emit = chunks_to_emit
        self._transcript_text = transcript_text
        self._stopped = False

    def start(self, session_dir: Path, on_chunk: AudioChunkCallback) -> None:
        """Emite <chunks_to_emit> WAVs sintéticos mínimos de inmediato."""
        for i in range(self._chunks_to_emit):
            wav_path = session_dir / f"audio_chunk_{i:04d}.wav"
            _write_minimal_wav(wav_path)
            on_chunk(wav_path)

    def stop(self) -> None:
        self._stopped = True


def _write_minimal_wav(path: Path) -> None:
    """Escribe un WAV válido con una muestra de silencio (44 bytes header + 2 data)."""
    import struct  # noqa: PLC0415

    sample_rate = 16000
    num_channels = 1
    bits_per_sample = 16
    num_samples = 1
    data_size = num_samples * num_channels * (bits_per_sample // 8)
    block_align = num_channels * (bits_per_sample // 8)
    byte_rate = sample_rate * block_align

    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, num_channels, sample_rate,
                            byte_rate, block_align, bits_per_sample))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(b"\x00" * data_size)
