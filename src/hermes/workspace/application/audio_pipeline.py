"""AudioPipeline — captura → VAD → transcripción → asociación a StepRecord (T088).

Pipeline:
    AudioCapturePort
        → VAD chunking (ventanas asimétricas -8s/+4s respecto al step, research §11)
        → TranscriptionPort (async, no bloquea captura)
        → asociación del transcript al StepRecord correspondiente

Invariantes:
- FR-040: el audio raw se DESCARTA tras la transcripción. Se verifica via
  métricas (``audio_chunk_discarded_total``).
- Si Whisper falla → reintentar 3 veces; en el 4º fallo, descartar el chunk
  y marcar el step con ``audio_transcript_lang=null, audio_transcript_confidence=null``.
- Constitución IV: ``confidence < 0.6`` → marcar ``low_confidence``.
  El transcript low_confidence NO se usa para inferir DecisionRules.

Métricas:
- ``audio_chunk_discarded_total``: contador de chunks descartados (FR-040).

FR-009, FR-010, FR-040, NFR-002, research §11.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from hermes.workspace.domain.ports.audio_capture_port import (
    AudioCapturePort,
    AudioChunk,
)
from hermes.training.domain.ports.transcription_port import (
    TranscriptResult,
    TranscriptionEngineDown,
    TranscriptionPort,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AudioPipeline",
    "AudioPipelineConfig",
    "StepRecord",
    "TranscriptAssociation",
]

_MAX_RETRIES = 3
_LOW_CONFIDENCE_THRESHOLD = 0.6


@dataclass
class StepRecord:
    """Referencia mínima de un StepRecord para la asociación temporal.

    La entidad completa vive en el bounded context de training.
    Este stub es suficiente para la pipeline de audio.
    """

    step_id: UUID
    event_ts_ms: int  # timestamp del evento DOM en la VM


@dataclass
class TranscriptAssociation:
    """Resultado de la asociación chunk → step."""

    chunk_id: UUID
    step_id: UUID | None
    transcript_text: str | None
    transcript_language: str | None
    transcript_confidence: float | None
    low_confidence: bool
    associated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass(frozen=True, slots=True)
class AudioPipelineConfig:
    """Parámetros de la pipeline."""

    step_window_pre_ms: int = 8_000   # -8 s antes del step (research §11)
    step_window_post_ms: int = 4_000  # +4 s después del step
    max_retries: int = _MAX_RETRIES
    low_confidence_threshold: float = _LOW_CONFIDENCE_THRESHOLD
    retry_base_delay_s: float = 0.5   # base para backoff entre reintentos


class AudioPipeline:
    """Orquesta captura → VAD → transcripción → asociación.

    Diseño:
    - ``run()`` corre como tarea async en paralelo con el capture.
    - La transcripción es no bloqueante: se lanza como asyncio.Task por cada chunk.
    - El audio raw se descarta SIEMPRE tras la transcripción (FR-040).
    - Las asociaciones se acumulan en ``_associations`` y se pueden leer via
      ``associations()``.
    """

    def __init__(
        self,
        *,
        capture: AudioCapturePort,
        transcription: TranscriptionPort,
        config: AudioPipelineConfig | None = None,
        on_association: Callable[[TranscriptAssociation], Any] | None = None,
    ) -> None:
        self._capture = capture
        self._transcription = transcription
        self._cfg = config or AudioPipelineConfig()
        self._on_association = on_association
        self._steps: list[StepRecord] = []
        self._associations: list[TranscriptAssociation] = []
        self._chunk_discarded_total: int = 0
        self._running = False
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        training_session_id: UUID,
        tenant_id: UUID,
    ) -> None:
        """Abre captura y arranca la pipeline en background."""
        await self._capture.open(
            training_session_id=training_session_id,
            tenant_id=tenant_id,
        )
        self._running = True
        self._task = asyncio.create_task(self._pipeline_loop())

    async def stop(self) -> None:
        """Para la pipeline y cierra el device."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._capture.close()

    # ------------------------------------------------------------------
    # Step registry
    # ------------------------------------------------------------------

    def register_step(self, step: StepRecord) -> None:
        """Registra un StepRecord capturado (llamado desde el training orchestrator)."""
        self._steps.append(step)

    def associations(self) -> list[TranscriptAssociation]:
        return list(self._associations)

    @property
    def chunks_discarded_total(self) -> int:
        return self._chunk_discarded_total

    # ------------------------------------------------------------------
    # Pipeline loop
    # ------------------------------------------------------------------

    async def _pipeline_loop(self) -> None:
        transcription_tasks: list[asyncio.Task[None]] = []
        async for chunk in self._capture.chunks():
            if not self._running:
                break
            task = asyncio.create_task(self._transcribe_and_associate(chunk))
            transcription_tasks.append(task)
        # Esperar las transcripciones en vuelo al cerrar.
        if transcription_tasks:
            await asyncio.gather(*transcription_tasks, return_exceptions=True)

    async def _transcribe_and_associate(self, chunk: AudioChunk) -> None:
        """Transcribe el chunk, descarta el audio raw, asocia al step. FR-040."""
        result = await self._transcribe_with_retry(chunk)
        # FR-040: el audio raw ya no es necesario tras la transcripción.
        # El objeto AudioChunk es inmutable (frozen dataclass), así que el
        # "descarte" se refleja en no almacenar la referencia al chunk y
        # en incrementar el contador.
        self._chunk_discarded_total += 1
        logger.debug(
            "audio_pipeline.chunk_discarded",
            extra={
                "chunk_id": str(chunk.chunk_id),
                "audio_chunk_discarded_total": self._chunk_discarded_total,
            },
        )

        association = self._associate_to_step(chunk, result)
        self._associations.append(association)
        logger.debug(
            "audio_pipeline.associated",
            extra={
                "chunk_id": str(chunk.chunk_id),
                "step_id": str(association.step_id) if association.step_id else None,
                "confidence": association.transcript_confidence,
                "low_confidence": association.low_confidence,
            },
        )
        if self._on_association is not None:
            cb = self._on_association(association)
            if asyncio.iscoroutine(cb):
                await cb

    async def _transcribe_with_retry(
        self, chunk: AudioChunk
    ) -> TranscriptResult | None:
        """Reintenta hasta _MAX_RETRIES; en caso de fallo total retorna None."""
        last_exc: Exception | None = None
        for attempt in range(1, self._cfg.max_retries + 1):
            try:
                result = await self._transcription.transcribe(chunk)
                return result
            except TranscriptionEngineDown as exc:
                last_exc = exc
                logger.warning(
                    "audio_pipeline.transcription_retry",
                    extra={
                        "chunk_id": str(chunk.chunk_id),
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )
                await asyncio.sleep(self._cfg.retry_base_delay_s * attempt)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "audio_pipeline.transcription_error",
                    extra={"chunk_id": str(chunk.chunk_id), "error": str(exc)},
                )
                break

        logger.error(
            "audio_pipeline.transcription_failed_discarding",
            extra={
                "chunk_id": str(chunk.chunk_id),
                "error": str(last_exc) if last_exc else "unknown",
            },
        )
        return None

    def _associate_to_step(
        self,
        chunk: AudioChunk,
        result: TranscriptResult | None,
    ) -> TranscriptAssociation:
        """Asocia el chunk al step mediante ventana asimétrica -8s/+4s (research §11)."""
        step_id = self._find_best_step(chunk)
        if result is None:
            return TranscriptAssociation(
                chunk_id=chunk.chunk_id,
                step_id=step_id,
                transcript_text=None,
                transcript_language=None,
                transcript_confidence=None,
                low_confidence=False,
            )
        low_confidence = result.confidence < self._cfg.low_confidence_threshold
        return TranscriptAssociation(
            chunk_id=chunk.chunk_id,
            step_id=step_id,
            transcript_text=result.text,
            transcript_language=result.language.value,
            transcript_confidence=result.confidence,
            low_confidence=low_confidence,
        )

    def _find_best_step(self, chunk: AudioChunk) -> UUID | None:
        """Algoritmo de asociación asimétrico -8s/+4s.

        Un chunk se asocia al step cuya ventana
        [event_ts - 8000ms, event_ts + 4000ms] solape más con el chunk.
        Empate → al paso anterior.
        """
        chunk_start = chunk.start_offset_ms
        chunk_end = chunk.end_offset_ms
        pre = self._cfg.step_window_pre_ms
        post = self._cfg.step_window_post_ms

        best_step_id: UUID | None = None
        best_overlap: int = 0
        best_step_ts: int = -1

        for step in self._steps:
            window_start = step.event_ts_ms - pre
            window_end = step.event_ts_ms + post
            overlap = max(0, min(chunk_end, window_end) - max(chunk_start, window_start))
            if overlap > best_overlap or (
                overlap == best_overlap and step.event_ts_ms <= best_step_ts
            ):
                best_overlap = overlap
                best_step_id = step.step_id
                best_step_ts = step.event_ts_ms

        return best_step_id
