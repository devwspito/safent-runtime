"""SileroVadAdapter — VAD chunking (T090).

Implementa un puerto VAD simple para detectar actividad de voz en PCM.

Lazy-import de torch y silero-vad: importar este módulo sin las libs
instaladas NO falla; sólo falla al llamar is_speech().

El VAD corre dentro del netns "whisper" sin acceso de red (research §3).
"""

from __future__ import annotations

import logging
import struct
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["SileroVadAdapter", "VadConfig"]

_SAMPLE_RATE = 16_000
_SILERO_VAD_THRESHOLD = 0.5


class VadError(RuntimeError):
    """Base."""


class VadModelUnavailable(VadError):
    """torch o silero-vad no están instalados."""


class VadConfig:
    """Parámetros del VAD."""

    def __init__(
        self,
        *,
        sample_rate: int = _SAMPLE_RATE,
        threshold: float = _SILERO_VAD_THRESHOLD,
    ) -> None:
        self.sample_rate = sample_rate
        self.threshold = threshold


class SileroVadAdapter:
    """Adapter VAD sobre Silero-VAD.

    Uso típico::

        vad = SileroVadAdapter()
        if vad.is_speech(pcm_bytes):
            # chunk contiene voz
    """

    def __init__(self, config: VadConfig | None = None) -> None:
        self._cfg = config or VadConfig()
        self._model: Any = None
        self._get_speech_timestamps: Any = None

    def is_speech(self, chunk_pcm: bytes) -> bool:
        """Detecta si el chunk PCM contiene actividad de voz.

        chunk_pcm: bytes PCM 16kHz mono int16 little-endian.

        Lanza VadModelUnavailable si torch/silero-vad no disponibles.
        """
        model, get_speech_timestamps = self._ensure_model()
        audio_tensor = self._pcm_to_tensor(chunk_pcm)
        speech_ts = get_speech_timestamps(
            audio_tensor,
            model,
            threshold=self._cfg.threshold,
            sampling_rate=self._cfg.sample_rate,
        )
        return len(speech_ts) > 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_model(self) -> tuple[Any, Any]:
        """Lazy-init del modelo Silero-VAD."""
        if self._model is not None:
            return self._model, self._get_speech_timestamps

        try:
            import torch  # noqa: PLC0415
        except ImportError as exc:
            raise VadModelUnavailable(
                "torch no está instalado. Instala silero-vad con: "
                "pip install torch silero-vad"
            ) from exc

        try:
            from silero_vad import load_silero_vad, get_speech_timestamps  # noqa: PLC0415

            model = load_silero_vad()
        except ImportError:
            # Fallback: usar el hub de torch.
            try:
                model, utils = torch.hub.load(
                    repo_or_dir="snakers4/silero-vad",
                    model="silero_vad",
                    force_reload=False,
                    onnx=False,
                )
                get_speech_timestamps = utils[0]
            except Exception as exc:
                raise VadModelUnavailable(
                    f"No se pudo cargar silero-vad: {exc}"
                ) from exc

        self._model = model
        self._get_speech_timestamps = get_speech_timestamps
        logger.info("silero_vad.model_loaded")
        return model, get_speech_timestamps

    def _pcm_to_tensor(self, chunk_pcm: bytes) -> Any:
        """Convierte PCM int16 LE a tensor float32 PyTorch."""
        import torch  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        num_samples = len(chunk_pcm) // 2
        samples = struct.unpack(f"<{num_samples}h", chunk_pcm)
        audio_array = np.array(samples, dtype=np.float32) / 32768.0
        return torch.tensor(audio_array)
