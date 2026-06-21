"""FasterWhisperBackend — Whisper local con faster-whisper (research §11).

Cumple `WhisperBackendPort`. Import lazy de `faster_whisper` para
permitir tests sin la dependencia. Modelo por defecto: distil-large-v3
(precarga ~750 MB) en `/opt/models/distil-large-v3/`.

Comportamiento:
  - load: la primera llamada inicializa el modelo (~5s en aarch64).
  - transcribe: bloqueante; el worker (WhisperWorker) lo invoca en su
    thread.
  - device auto-detect: CUDA si disponible, sino CPU.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MODEL_PATH = Path("/opt/models/distil-large-v3")
_DEFAULT_COMPUTE_TYPE = "int8"  # cuantizado por defecto en aarch64


@dataclass(slots=True)
class FasterWhisperBackend:
    """Backend real — instancia `WhisperModel` lazy."""

    model_path: Path = _DEFAULT_MODEL_PATH
    compute_type: str = _DEFAULT_COMPUTE_TYPE
    device: str = "auto"
    beam_size: int = 5
    _model: Any | None = field(default=None, init=False)

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "faster-whisper no instalado — instala con "
                "pip install faster-whisper"
            ) from exc
        logger.info(
            "Loading faster-whisper model path=%s device=%s compute=%s",
            self.model_path,
            self.device,
            self.compute_type,
        )
        self._model = WhisperModel(
            str(self.model_path),
            device=self.device,
            compute_type=self.compute_type,
        )
        return self._model

    def transcribe(
        self,
        *,
        audio_path: Path,
        language_hint: str,
    ) -> tuple[str, str, float]:
        model = self._ensure_model()
        language = None if language_hint == "auto" else language_hint
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language,
            beam_size=self.beam_size,
            vad_filter=True,
        )
        segments = list(segments_iter)
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, info.language, info.duration


def is_available() -> bool:
    """Verifica si faster-whisper se puede importar (sin cargar modelo)."""
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False
