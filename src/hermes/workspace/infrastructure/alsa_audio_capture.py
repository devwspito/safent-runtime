"""AlsaAudioCaptureAdapter — captura mic via PulseAudio + ALSA (T087).

Cumple ``AudioCapturePort``.

Diseño:
- Captura PCM 24 kHz mono 16-bit (re-sampled por parec/PulseAudio a 24 kHz).
  El contrato del puerto declara 16 kHz — usamos 24 kHz internamente y
  lo marcamos en el AudioChunk para que el caller (AudioPipeline) lo sepa.
  Faster-Whisper acepta cualquier sample rate y re-samplea internamente.
- Usa ``subprocess`` a ``parec`` (PulseAudio raw capture) o ``pulseaudio-client``
  via socket Unix. Lazy-import: importar este módulo sin parec disponible
  no falla; solo falla al llamar ``open()``.
- El proceso ``parec`` se arranca con ``asyncio.create_subprocess_exec``.
  El output stdout es PCM raw que se lee en chunks de CHUNK_BYTES.
- Al ``close()``, se manda SIGTERM a parec y se drena el buffer.

Constitución IV (fail-closed):
- Device no disponible → ``AudioDeviceUnavailable``.
- Permiso denegado (parec retorna rc=1 con stderr "access denied") →
  ``MicrophonePermissionDenied``.

FR-006, FR-009, FR-010, FR-040.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from hermes.workspace.domain.ports.audio_capture_port import (
    AudioChunk,
    AudioCaptureError,
    AudioDeviceUnavailable,
    CaptureState,
    MicrophonePermissionDenied,
)

logger = logging.getLogger(__name__)

__all__ = ["AlsaAudioCaptureAdapter", "AlsaConfig"]

# PCM chunk: 24 kHz × 1 ch × 2 bytes × 0.5 s = 24000 bytes
_SAMPLE_RATE = 24_000
_CHANNELS = 1
_SAMPLE_WIDTH_BYTES = 2
_CHUNK_DURATION_S = 0.5
_CHUNK_BYTES = int(_SAMPLE_RATE * _CHANNELS * _SAMPLE_WIDTH_BYTES * _CHUNK_DURATION_S)


@dataclass(frozen=True, slots=True)
class AlsaConfig:
    """Configuración del adapter inyectada en boot."""

    pulse_source: str = "default"   # nombre del source de PulseAudio
    sample_rate_hz: int = _SAMPLE_RATE
    channels: int = _CHANNELS


class AlsaAudioCaptureAdapter:
    """Adapter de captura ALSA/PulseAudio. Cumple ``AudioCapturePort``."""

    def __init__(self, config: AlsaConfig | None = None) -> None:
        self._cfg = config or AlsaConfig()
        self._state = CaptureState.IDLE
        self._proc: asyncio.subprocess.Process | None = None  # type: ignore[name-defined]
        self._training_session_id: UUID | None = None
        self._tenant_id: UUID | None = None
        self._session_start_ms: int = 0

    # ------------------------------------------------------------------
    # AudioCapturePort
    # ------------------------------------------------------------------

    async def open(
        self,
        *,
        training_session_id: UUID,
        tenant_id: UUID,
    ) -> None:
        """Arranca parec. Fail-closed ante permiso o device no disponibles."""
        if self._state == CaptureState.CAPTURING:
            return

        self._training_session_id = training_session_id
        self._tenant_id = tenant_id
        self._session_start_ms = int(datetime.now(tz=UTC).timestamp() * 1000)

        try:
            self._proc = await self._spawn_parec()
        except FileNotFoundError as exc:
            raise AudioDeviceUnavailable(
                "parec no encontrado. ¿PulseAudio instalado dentro de Hermes OS?"
            ) from exc
        except PermissionError as exc:
            raise MicrophonePermissionDenied("Permiso de micrófono denegado") from exc

        # Esperar brevemente; si el proceso muere al instante → error.
        await asyncio.sleep(0.1)
        if self._proc.returncode is not None:
            stderr = b""
            if self._proc.stderr is not None:
                stderr = await self._proc.stderr.read(512)
            if b"access denied" in stderr.lower() or b"Permission" in stderr:
                raise MicrophonePermissionDenied(
                    f"PulseAudio denegó acceso al device: {stderr[:200]!r}"
                )
            raise AudioDeviceUnavailable(
                f"parec terminó inmediatamente (rc={self._proc.returncode}): {stderr[:200]!r}"
            )

        self._state = CaptureState.CAPTURING
        logger.info(
            "alsa_audio_capture.opened",
            extra={
                "training_session_id": str(training_session_id),
                "source": self._cfg.pulse_source,
                "sample_rate": self._cfg.sample_rate_hz,
            },
        )

    async def chunks(self) -> AsyncIterator[AudioChunk]:  # type: ignore[override]
        """Stream pull de chunks PCM raw. Cancela cuando el caller cierra."""
        if self._proc is None or self._proc.stdout is None:
            raise AudioDeviceUnavailable("Capture no abierto; llama a open() primero")

        session_id = self._training_session_id
        tenant_id = self._tenant_id

        assert session_id is not None
        assert tenant_id is not None

        offset_ms = 0
        while True:
            try:
                data = await self._proc.stdout.readexactly(_CHUNK_BYTES)
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    yield self._make_chunk(exc.partial, session_id, tenant_id, offset_ms)
                break
            except asyncio.CancelledError:
                break

            chunk_duration_ms = int(_CHUNK_DURATION_S * 1000)
            yield self._make_chunk(data, session_id, tenant_id, offset_ms)
            offset_ms += chunk_duration_ms

    async def close(self) -> None:
        """Cierra el device y termina parec. Idempotente."""
        if self._state == CaptureState.CLOSED:
            return
        self._state = CaptureState.CLOSED
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._proc.kill()
        logger.info(
            "alsa_audio_capture.closed",
            extra={"training_session_id": str(self._training_session_id)},
        )

    @property
    def state(self) -> CaptureState:
        return self._state

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _spawn_parec(self) -> asyncio.subprocess.Process:  # type: ignore[name-defined]
        return await asyncio.create_subprocess_exec(
            "parec",
            "--device", self._cfg.pulse_source,
            "--rate", str(self._cfg.sample_rate_hz),
            "--channels", str(self._cfg.channels),
            "--format", "s16le",
            "--raw",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    def _make_chunk(
        self,
        data: bytes,
        session_id: UUID,
        tenant_id: UUID,
        start_offset_ms: int,
    ) -> AudioChunk:
        chunk_duration_ms = int(len(data) / (_SAMPLE_RATE * _CHANNELS * _SAMPLE_WIDTH_BYTES) * 1000)
        return AudioChunk(
            chunk_id=uuid4(),
            training_session_id=session_id,
            tenant_id=tenant_id,
            audio_bytes=data,
            sample_rate_hz=self._cfg.sample_rate_hz,
            channels=self._cfg.channels,
            start_offset_ms=start_offset_ms,
            end_offset_ms=start_offset_ms + chunk_duration_ms,
        )
