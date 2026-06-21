"""AudioCapturePort — contrato de captura de audio dentro del Workspace.

T075 — implementado en src/ desde el contrato de spec 002.

Cubre: FR-006, FR-009, FR-010, FR-040.

Constitución IV: fail-closed — captura falla a media sesión → StepRecords
siguen capturándose, sesión marca partial_narrative=true.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID


class AudioCaptureError(RuntimeError):
    """Base."""


class MicrophonePermissionDenied(AudioCaptureError):
    """Formador rechazó el permiso de micrófono.

    La sesión continúa en modo silencioso (FR-006 + edge case).
    """


class AudioDeviceUnavailable(AudioCaptureError):
    """Sin device de audio dentro de la VM."""


class CaptureState(StrEnum):
    IDLE = "idle"
    CAPTURING = "capturing"
    SUSPENDED = "suspended"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """Chunk PCM del micrófono. Sólo en tránsito dentro de la VM.

    audio_bytes: PCM 16kHz mono int16 little-endian (formato de Whisper).
    Tras transcripción, se descarta inmediatamente (FR-040).
    """

    chunk_id: UUID
    training_session_id: UUID
    tenant_id: UUID
    audio_bytes: bytes
    sample_rate_hz: int = 16000
    channels: int = 1
    start_offset_ms: int = 0
    end_offset_ms: int = 0
    captured_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@runtime_checkable
class AudioCapturePort(Protocol):
    """Puerto de captura de audio dentro del Workspace.

    Implementaciones esperadas:
      - AlsaAudioCaptureAdapter (Hermes OS runtime).
      - InMemoryAudioCapture (tests).
    """

    async def open(
        self,
        *,
        training_session_id: UUID,
        tenant_id: UUID,
    ) -> None: ...

    def chunks(self) -> AsyncIterator[AudioChunk]: ...

    async def close(self) -> None: ...

    @property
    def state(self) -> CaptureState: ...
