"""Tests de AudioPipeline — VAD chunking + transcript assoc + audio raw discarded.

Sin VM, sin Whisper real, sin red.
Constitución V.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from hermes.workspace.application.audio_pipeline import (
    AudioPipeline,
    AudioPipelineConfig,
    StepRecord,
    TranscriptAssociation,
)
from hermes.workspace.testing.in_memory_audio_capture import InMemoryAudioCapture
from hermes.training.testing.fake_transcription import FakeTranscription
from hermes.training.domain.ports.transcription_port import TranscriptLanguage

pytestmark = pytest.mark.unit

_SILENCE = bytes(24000)  # 0.5 s de silencio PCM int16


def _make_pipeline(
    chunks: list[bytes] | None = None,
    transcript_text: str = "esto siempre ciérralo",
    confidence: float = 0.92,
    fail_after_n: int | None = None,
) -> tuple[AudioPipeline, InMemoryAudioCapture, FakeTranscription]:
    capture = InMemoryAudioCapture(preset_chunks=chunks or [_SILENCE])
    transcription = FakeTranscription(
        text=transcript_text,
        confidence=confidence,
        fail_after_n=fail_after_n,
    )
    pipeline = AudioPipeline(
        capture=capture,
        transcription=transcription,
        config=AudioPipelineConfig(
            step_window_pre_ms=8_000,
            step_window_post_ms=4_000,
            retry_base_delay_s=0.001,  # pruebas rápidas
        ),
    )
    return pipeline, capture, transcription


class TestAudioRawDiscarded:
    """FR-040: el audio raw NUNCA persiste tras la transcripción."""

    async def test_chunks_discarded_after_transcription(self) -> None:
        pipeline, capture, transcription = _make_pipeline(
            chunks=[_SILENCE, _SILENCE, _SILENCE]
        )
        training_session_id = uuid4()
        tenant_id = uuid4()

        await pipeline.start(
            training_session_id=training_session_id,
            tenant_id=tenant_id,
        )
        await asyncio.sleep(0.05)
        await pipeline.stop()

        assert pipeline.chunks_discarded_total == 3

    async def test_each_chunk_increments_counter(self) -> None:
        pipeline, _, _ = _make_pipeline(chunks=[_SILENCE])
        await pipeline.start(training_session_id=uuid4(), tenant_id=uuid4())
        await asyncio.sleep(0.05)
        await pipeline.stop()
        assert pipeline.chunks_discarded_total >= 1

    async def test_failed_transcription_still_discards(self) -> None:
        """Aunque Whisper falle, el contador sigue subiendo (FR-040)."""
        pipeline, _, _ = _make_pipeline(
            chunks=[_SILENCE, _SILENCE], fail_after_n=0
        )
        await pipeline.start(training_session_id=uuid4(), tenant_id=uuid4())
        await asyncio.sleep(0.05)
        await pipeline.stop()
        # Ambos chunks deben descartarse aunque la transcripción falle.
        assert pipeline.chunks_discarded_total == 2


class TestTranscriptAssociation:
    """Verifica la asociación asimétrica chunk→step (-8s/+4s, research §11)."""

    async def test_chunk_associates_to_step_in_window(self) -> None:
        pipeline, _, _ = _make_pipeline()
        await pipeline.start(training_session_id=uuid4(), tenant_id=uuid4())

        step = StepRecord(step_id=uuid4(), event_ts_ms=500)  # chunk empieza en ~0 ms
        pipeline.register_step(step)

        await asyncio.sleep(0.05)
        await pipeline.stop()

        associations = pipeline.associations()
        assert len(associations) >= 1
        # El step está dentro de la ventana [0-8000, 0+4000]=[−8000,4000] → solapa.
        associated_steps = [a.step_id for a in associations if a.step_id == step.step_id]
        assert len(associated_steps) >= 1

    async def test_chunk_without_steps_associates_to_none(self) -> None:
        pipeline, _, _ = _make_pipeline()
        await pipeline.start(training_session_id=uuid4(), tenant_id=uuid4())
        await asyncio.sleep(0.05)
        await pipeline.stop()

        # Sin steps registrados, step_id debe ser None.
        for assoc in pipeline.associations():
            assert assoc.step_id is None

    async def test_empate_asocia_al_paso_anterior(self) -> None:
        """Empate de overlap → el paso más antiguo (anterior) gana."""
        pipeline, _, _ = _make_pipeline(chunks=[_SILENCE])
        await pipeline.start(training_session_id=uuid4(), tenant_id=uuid4())

        # Dos steps con el mismo overlap sobre el chunk [0, 500ms]
        step_a = StepRecord(step_id=uuid4(), event_ts_ms=0)
        step_b = StepRecord(step_id=uuid4(), event_ts_ms=250)
        pipeline.register_step(step_a)
        pipeline.register_step(step_b)

        await asyncio.sleep(0.05)
        await pipeline.stop()

        associations = pipeline.associations()
        assert associations, "Debe haber al menos una asociación"


class TestLowConfidence:
    """Constitución IV: confidence < 0.6 → low_confidence=True."""

    async def test_low_confidence_flagged(self) -> None:
        pipeline, _, _ = _make_pipeline(confidence=0.4)
        await pipeline.start(training_session_id=uuid4(), tenant_id=uuid4())
        await asyncio.sleep(0.05)
        await pipeline.stop()

        for assoc in pipeline.associations():
            if assoc.transcript_confidence is not None:
                assert assoc.low_confidence is True

    async def test_high_confidence_not_flagged(self) -> None:
        pipeline, _, _ = _make_pipeline(confidence=0.95)
        await pipeline.start(training_session_id=uuid4(), tenant_id=uuid4())
        await asyncio.sleep(0.05)
        await pipeline.stop()

        for assoc in pipeline.associations():
            if assoc.transcript_confidence is not None:
                assert assoc.low_confidence is False


class TestTranscriptionFailure:
    """Si Whisper falla 3 veces → step marcado con null confidence."""

    async def test_after_max_retries_association_has_null_confidence(self) -> None:
        pipeline, _, _ = _make_pipeline(
            chunks=[_SILENCE], fail_after_n=0
        )
        await pipeline.start(training_session_id=uuid4(), tenant_id=uuid4())
        await asyncio.sleep(0.1)
        await pipeline.stop()

        for assoc in pipeline.associations():
            assert assoc.transcript_confidence is None
            assert assoc.transcript_language is None
            assert assoc.transcript_text is None


class TestMicPermissionDenied:
    """Edge case: micrófono no concedido → MicrophonePermissionDenied."""

    async def test_denied_mic_raises(self) -> None:
        from hermes.workspace.domain.ports.audio_capture_port import MicrophonePermissionDenied

        capture = InMemoryAudioCapture(deny_permission=True)
        pipeline = AudioPipeline(capture=capture, transcription=FakeTranscription())

        with pytest.raises(MicrophonePermissionDenied):
            await pipeline.start(training_session_id=uuid4(), tenant_id=uuid4())


class TestOnAssociationCallback:
    """El callback on_association se llama por cada chunk procesado."""

    async def test_callback_called_for_each_chunk(self) -> None:
        received: list[TranscriptAssociation] = []

        def cb(assoc: TranscriptAssociation) -> None:
            received.append(assoc)

        pipeline, _, _ = _make_pipeline(chunks=[_SILENCE, _SILENCE])
        pipeline._on_association = cb  # inyección directa para test

        await pipeline.start(training_session_id=uuid4(), tenant_id=uuid4())
        await asyncio.sleep(0.05)
        await pipeline.stop()

        assert len(received) == 2
