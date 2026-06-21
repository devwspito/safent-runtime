"""FakeTranscription — fake de TranscriptionPort para tests (T008, US1).

Constitución V: cualquier puerto tiene un fake en testing/ que corre sin
VM, sin Whisper, sin GPU.

Retorna transcripts configurados en la construcción o un default fijo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from hermes.workspace.domain.ports.audio_capture_port import AudioChunk
from hermes.training.domain.ports.transcription_port import (
    TranscriptLanguage,
    TranscriptResult,
    TranscriptionEngineDown,
    TranscriptionPort,
)

__all__ = ["FakeTranscription"]

_DEFAULT_TEXT = "esto siempre ciérralo"
_DEFAULT_LANG = TranscriptLanguage.SPANISH
_DEFAULT_CONFIDENCE = 0.92


class FakeTranscription:
    """Fake de TranscriptionPort.

    Uso en tests::

        fake = FakeTranscription(text="siempre acepta", confidence=0.95)
        # o para simular engine down:
        fake = FakeTranscription(fail_after_n=2)
    """

    _engine_name = "fake_transcription"
    _supported_languages: frozenset[TranscriptLanguage] = frozenset(
        {
            TranscriptLanguage.SPANISH,
            TranscriptLanguage.CATALAN,
            TranscriptLanguage.GALICIAN,
            TranscriptLanguage.BASQUE,
            TranscriptLanguage.ENGLISH,
        }
    )

    def __init__(
        self,
        *,
        text: str = _DEFAULT_TEXT,
        language: TranscriptLanguage = _DEFAULT_LANG,
        confidence: float = _DEFAULT_CONFIDENCE,
        low_confidence: bool = False,
        fail_after_n: int | None = None,
    ) -> None:
        self._text = text
        self._language = language
        self._confidence = confidence
        self._low_confidence = low_confidence
        self._fail_after_n = fail_after_n
        self._call_count = 0
        self.transcribed_chunks: list[AudioChunk] = []

    async def transcribe(self, chunk: AudioChunk) -> TranscriptResult:
        self._call_count += 1
        if self._fail_after_n is not None and self._call_count > self._fail_after_n:
            raise TranscriptionEngineDown("Fake: motor caído")
        self.transcribed_chunks.append(chunk)
        duration_ms = int(len(chunk.audio_bytes) / (16000 * 1 * 2) * 1000)
        low = self._low_confidence or self._confidence < 0.6
        return TranscriptResult(
            chunk_id=chunk.chunk_id,
            training_session_id=chunk.training_session_id,
            tenant_id=chunk.tenant_id,
            text=self._text,
            language=self._language,
            confidence=self._confidence,
            low_confidence=low,
            audio_duration_ms=max(duration_ms, 1),
        )

    @property
    def engine_name(self) -> str:
        return self._engine_name

    @property
    def supported_languages(self) -> frozenset[TranscriptLanguage]:
        return self._supported_languages

    @property
    def call_count(self) -> int:
        return self._call_count
