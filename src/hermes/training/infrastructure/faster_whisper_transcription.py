"""FasterWhisperTranscriptionAdapter — transcripción local con distil-large-v3 (T089).

Cumple ``TranscriptionPort``.

Diseño:
- Carga el modelo ``distil-large-v3`` CTranslate2 INT8 desde
  ``/opt/models/distil-large-v3/`` en el primer ``transcribe()`` (lazy init).
- Lazy-import de ``faster_whisper``: importar este módulo sin la lib instalada
  NO falla; sólo falla al llamar ``transcribe()``.
- Detección automática de idioma limitada al set soportado MVP.
- NFR-002: p95 ≤ 3s para chunks ≤ 10s. Objetivo alcanzable con distil-large-v3
  INT8 en 4 vCPU.
- FR-040: el audio raw del chunk se descarta explícitamente tras convertirlo
  a array numpy; el chunk original NO se guarda en memoria después del procesado.
- Constitución IV: confidence < 0.6 → low_confidence=True.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import tempfile
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from hermes.training.domain.ports.transcription_port import (
    TranscriptLanguage,
    TranscriptResult,
    TranscriptSegment,
    TranscriptionEngineDown,
    TranscriptionLanguageUnsupported,
    TranscriptionPort,
)
from hermes.workspace.domain.ports.audio_capture_port import AudioChunk

logger = logging.getLogger(__name__)

__all__ = ["FasterWhisperTranscriptionAdapter", "WhisperConfig"]

_DEFAULT_MODEL_PATH = Path("/opt/models/distil-large-v3")
_LOW_CONFIDENCE_THRESHOLD = 0.6

# BCP-47 code → TranscriptLanguage mapping (spec: es-ES / ca-ES / gl-ES / eu-ES / en-US)
_LANGUAGE_MAP: dict[str, TranscriptLanguage] = {
    "es": TranscriptLanguage.SPANISH,
    "ca": TranscriptLanguage.CATALAN,
    "gl": TranscriptLanguage.GALICIAN,
    "eu": TranscriptLanguage.BASQUE,
    "en": TranscriptLanguage.ENGLISH,
}


@dataclass(frozen=True, slots=True)
class WhisperConfig:
    """Configuración del adapter inyectada en boot."""

    model_path: Path = _DEFAULT_MODEL_PATH
    device: str = "cpu"            # "cpu" | "cuda"
    compute_type: str = "int8"     # CTranslate2 INT8
    beam_size: int = 4
    condition_on_previous_text: bool = False
    low_confidence_threshold: float = _LOW_CONFIDENCE_THRESHOLD


class FasterWhisperTranscriptionAdapter:
    """Adapter Faster-Whisper distil-large-v3. Cumple ``TranscriptionPort``."""

    _engine_name = "faster_whisper_distil_large_v3"
    _supported_languages: frozenset[TranscriptLanguage] = frozenset(
        {
            TranscriptLanguage.SPANISH,
            TranscriptLanguage.CATALAN,
            TranscriptLanguage.GALICIAN,
            TranscriptLanguage.BASQUE,
            TranscriptLanguage.ENGLISH,
        }
    )

    def __init__(self, config: WhisperConfig | None = None) -> None:
        self._cfg = config or WhisperConfig()
        self._model: Any = None  # faster_whisper.WhisperModel; lazy

    # ------------------------------------------------------------------
    # TranscriptionPort
    # ------------------------------------------------------------------

    async def transcribe(self, chunk: AudioChunk) -> TranscriptResult:
        """Transcribe un chunk. Descarta el audio raw tras procesar (FR-040).

        Fail-closed: motor caído → TranscriptionEngineDown.
        """
        model = self._ensure_model()
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, self._run_transcription, model, chunk
            )
        except Exception as exc:
            raise TranscriptionEngineDown(
                f"Faster-Whisper falló durante la transcripción: {exc}"
            ) from exc
        # FR-040: el audio raw ya se procesó; no guardamos referencia al chunk.
        return result

    @property
    def engine_name(self) -> str:
        return self._engine_name

    @property
    def supported_languages(self) -> frozenset[TranscriptLanguage]:
        return self._supported_languages

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_model(self) -> Any:
        """Lazy-init del modelo. Falla claro si faster_whisper no está disponible."""
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # noqa: PLC0415
        except ImportError as exc:
            raise TranscriptionEngineDown(
                "faster_whisper no está instalado. Instala el extra [workspace] "
                "o usa FakeTranscription en tests."
            ) from exc

        if not self._cfg.model_path.exists():
            raise TranscriptionEngineDown(
                f"Modelo Whisper no encontrado en {self._cfg.model_path}. "
                "¿Partición /opt/models montada?"
            )

        self._model = WhisperModel(
            str(self._cfg.model_path),
            device=self._cfg.device,
            compute_type=self._cfg.compute_type,
        )
        logger.info(
            "whisper.model_loaded",
            extra={
                "model_path": str(self._cfg.model_path),
                "device": self._cfg.device,
                "compute_type": self._cfg.compute_type,
            },
        )
        return self._model

    def _run_transcription(self, model: Any, chunk: AudioChunk) -> TranscriptResult:
        """Ejecutado en el threadpool. Convierte PCM → numpy → faster_whisper."""
        import numpy as np  # noqa: PLC0415

        # Convertir PCM int16 little-endian a float32 normalizado [-1, 1].
        num_samples = len(chunk.audio_bytes) // 2
        samples = struct.unpack(f"<{num_samples}h", chunk.audio_bytes)
        audio_array = np.array(samples, dtype=np.float32) / 32768.0

        # Si el sample rate no es 16 kHz, resamplear.
        if chunk.sample_rate_hz != 16_000:
            audio_array = self._resample(audio_array, chunk.sample_rate_hz, 16_000)

        # FR-040: audio_bytes del chunk ya no se necesita; el array es temporal.
        audio_duration_ms = int(len(audio_array) / 16_000 * 1000)

        # Transcribir con Faster-Whisper.
        segments_iter, info = model.transcribe(
            audio_array,
            beam_size=self._cfg.beam_size,
            condition_on_previous_text=self._cfg.condition_on_previous_text,
            language=None,  # auto-detect
        )

        detected_lang_short = info.language if info else "es"
        language = _LANGUAGE_MAP.get(detected_lang_short, TranscriptLanguage.SPANISH)
        if language not in self._supported_languages:
            raise TranscriptionLanguageUnsupported(
                f"Idioma detectado '{detected_lang_short}' fuera del set MVP. "
                f"Soportados: {sorted(self._supported_languages)}"
            )

        segments_list: list[TranscriptSegment] = []
        full_text_parts: list[str] = []
        confidence_sum = 0.0
        confidence_count = 0

        for seg in segments_iter:
            avg_logprob = getattr(seg, "avg_logprob", 0.0)
            seg_confidence = min(1.0, max(0.0, (avg_logprob + 1.0)))
            segments_list.append(
                TranscriptSegment(
                    start_s=seg.start,
                    end_s=seg.end,
                    text=seg.text.strip(),
                    confidence=seg_confidence,
                )
            )
            full_text_parts.append(seg.text.strip())
            confidence_sum += seg_confidence
            confidence_count += 1

        full_text = " ".join(full_text_parts)
        avg_confidence = confidence_sum / confidence_count if confidence_count > 0 else 0.0
        low_confidence = avg_confidence < self._cfg.low_confidence_threshold

        return TranscriptResult(
            chunk_id=chunk.chunk_id,
            training_session_id=chunk.training_session_id,
            tenant_id=chunk.tenant_id,
            text=full_text,
            language=language,
            confidence=avg_confidence,
            low_confidence=low_confidence,
            audio_duration_ms=audio_duration_ms,
            segments=tuple(segments_list),
        )

    @staticmethod
    def _resample(
        audio: "np.ndarray",  # type: ignore[name-defined]
        orig_rate: int,
        target_rate: int,
    ) -> "np.ndarray":  # type: ignore[name-defined]
        """Resamplea array float32. Lazy-import de scipy."""
        try:
            from scipy.signal import resample_poly  # noqa: PLC0415
            from math import gcd

            g = gcd(orig_rate, target_rate)
            return resample_poly(audio, target_rate // g, orig_rate // g)
        except ImportError:
            # Fallback lineal simple si scipy no está disponible.
            import numpy as np  # noqa: PLC0415

            ratio = target_rate / orig_rate
            new_len = int(len(audio) * ratio)
            return np.interp(
                np.linspace(0, len(audio) - 1, new_len),
                np.arange(len(audio)),
                audio,
            ).astype(np.float32)
