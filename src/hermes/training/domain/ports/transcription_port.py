"""TranscriptionPort — contrato de transcripción local de audio.

T075 — implementado en src/ desde el contrato de spec 002.

Cubre: FR-009, FR-010, FR-040, NFR-002.

Constitución III: transcript se tokeniza PII antes de cruzar a LLM upstream.
Constitución IV: confianza baja marca low_confidence y bloquea inferencia.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from hermes.workspace.domain.ports.audio_capture_port import AudioChunk


class TranscriptionError(RuntimeError):
    """Base."""


class TranscriptionEngineDown(TranscriptionError):
    """Motor local (Whisper) no responde — OOM o crash.

    El caller decide retry (edge case: sistema cae a "captura sin transcript").
    """


class TranscriptionLanguageUnsupported(TranscriptionError):
    """Idioma fuera del set MVP (es / ca / gl / eu / en)."""


class TranscriptLanguage(StrEnum):
    SPANISH = "es-ES"
    CATALAN = "ca-ES"
    GALICIAN = "gl-ES"
    BASQUE = "eu-ES"
    ENGLISH = "en-US"


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """Segmento temporal dentro de un chunk transcrito."""

    start_s: float
    end_s: float
    text: str
    confidence: float


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    """Resultado de transcribir un chunk.

    text: cleartext local pre-tokenización. NUNCA sale de la VM sin PII tokenizer.
    confidence: agregada del chunk; el caller marca low_confidence si bajo umbral.
    segments: lista de segmentos temporales (para asociación step).
    """

    chunk_id: UUID
    training_session_id: UUID
    tenant_id: UUID
    text: str
    language: TranscriptLanguage
    confidence: float
    low_confidence: bool
    audio_duration_ms: int
    segments: tuple[TranscriptSegment, ...] = ()
    transcribed_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@runtime_checkable
class TranscriptionPort(Protocol):
    """Puerto de transcripción local.

    Implementaciones esperadas:
      - FasterWhisperTranscriptionAdapter (MVP, dentro de Hermes OS, sin red).
      - FakeTranscription (tests; transcript fijo).
    """

    async def transcribe(self, chunk: AudioChunk) -> TranscriptResult: ...

    @property
    def engine_name(self) -> str: ...

    @property
    def supported_languages(self) -> frozenset[TranscriptLanguage]: ...
