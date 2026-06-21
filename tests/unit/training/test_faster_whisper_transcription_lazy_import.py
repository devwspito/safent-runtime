"""Tests del FasterWhisperTranscriptionAdapter — lazy-import guard.

Verifica que importar el módulo SIN faster_whisper instalado funciona,
y que solo falla en transcribe() (constitución V / lazy-import).

Sin VM, sin Whisper real, sin GPU.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.training.domain.ports.transcription_port import (
    TranscriptionEngineDown,
    TranscriptLanguage,
)
from hermes.workspace.domain.ports.audio_capture_port import AudioChunk

pytestmark = pytest.mark.unit


def _make_chunk() -> AudioChunk:
    return AudioChunk(
        chunk_id=uuid4(),
        training_session_id=uuid4(),
        tenant_id=uuid4(),
        audio_bytes=bytes(32000),  # 1 s a 16 kHz int16
        sample_rate_hz=16000,
        channels=1,
        start_offset_ms=0,
        end_offset_ms=1000,
    )


class TestModuleImportWithoutFasterWhisper:
    """Importar el módulo no debe lanzar ImportError aunque la lib falte."""

    def test_import_succeeds(self) -> None:
        # Si llega aquí, el import no explotó.
        from hermes.training.infrastructure.faster_whisper_transcription import (
            FasterWhisperTranscriptionAdapter,
        )

        assert FasterWhisperTranscriptionAdapter is not None

    def test_instantiation_succeeds(self) -> None:
        from hermes.training.infrastructure.faster_whisper_transcription import (
            FasterWhisperTranscriptionAdapter,
        )

        adapter = FasterWhisperTranscriptionAdapter()
        assert adapter is not None

    def test_engine_name_accessible_without_lib(self) -> None:
        from hermes.training.infrastructure.faster_whisper_transcription import (
            FasterWhisperTranscriptionAdapter,
        )

        adapter = FasterWhisperTranscriptionAdapter()
        assert adapter.engine_name == "faster_whisper_distil_large_v3"

    def test_supported_languages_accessible_without_lib(self) -> None:
        from hermes.training.infrastructure.faster_whisper_transcription import (
            FasterWhisperTranscriptionAdapter,
        )

        adapter = FasterWhisperTranscriptionAdapter()
        assert TranscriptLanguage.SPANISH in adapter.supported_languages
        assert TranscriptLanguage.CATALAN in adapter.supported_languages
        assert TranscriptLanguage.GALICIAN in adapter.supported_languages
        assert TranscriptLanguage.BASQUE in adapter.supported_languages
        assert TranscriptLanguage.ENGLISH in adapter.supported_languages


class TestTranscribeFailsWithoutFasterWhisper:
    """transcribe() falla con TranscriptionEngineDown si faster_whisper no instalado."""

    async def test_transcribe_raises_engine_down_without_lib(self) -> None:
        """Este test pasa si faster_whisper NO está instalado.

        Si faster_whisper SÍ está instalado pero el modelo no existe,
        TranscriptionEngineDown se lanza igualmente (modelo no encontrado).
        """
        from hermes.training.infrastructure.faster_whisper_transcription import (
            FasterWhisperTranscriptionAdapter,
        )

        adapter = FasterWhisperTranscriptionAdapter()
        chunk = _make_chunk()

        with pytest.raises(TranscriptionEngineDown):
            await adapter.transcribe(chunk)


class TestWhisperConfig:
    """Verifica la configuración por defecto del adapter."""

    def test_default_model_path(self) -> None:
        from hermes.training.infrastructure.faster_whisper_transcription import (
            FasterWhisperTranscriptionAdapter,
            WhisperConfig,
        )
        from pathlib import Path

        cfg = WhisperConfig()
        assert cfg.model_path == Path("/opt/models/distil-large-v3")

    def test_default_compute_type_int8(self) -> None:
        from hermes.training.infrastructure.faster_whisper_transcription import (
            WhisperConfig,
        )

        cfg = WhisperConfig()
        assert cfg.compute_type == "int8"

    def test_custom_config_applied(self) -> None:
        from hermes.training.infrastructure.faster_whisper_transcription import (
            FasterWhisperTranscriptionAdapter,
            WhisperConfig,
        )
        from pathlib import Path

        cfg = WhisperConfig(model_path=Path("/tmp/my-model"), device="cuda")
        adapter = FasterWhisperTranscriptionAdapter(cfg)
        assert adapter._cfg.model_path == Path("/tmp/my-model")
        assert adapter._cfg.device == "cuda"
