"""Screenshot + DOM snapshot VOs.

El `StepRecorder` persiste pre/post de cada step. El diff entre snapshots
sirve para:
  - Detectar drift de selectores (mismo step, DOM cambio fuera de lo esperado).
  - Audit defensible (ante una inspeccion AEAT, podemos demostrar que el
    formulario que se presento es el que se aprobo).
  - Re-entrenar al LLM cuando una sede actualiza su layout.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Screenshot:
    """Screenshot raw + metadata. NO incluye los bytes — solo referencia.

    Los bytes se persisten externamente (S3, filesystem encriptado, etc.).
    El `content_hash` SHA-256 permite verificar integridad y deduplicar.
    """

    screenshot_id: UUID
    step_id: UUID
    moment: Literal["pre", "post"]
    content_hash: str  # SHA-256 hex
    width_px: int
    height_px: int
    format: str = "png"  # "png" | "jpeg" | "webp"
    storage_uri: str = ""  # ej. "s3://hermes-artifacts/<tenant>/<step>.png"
    captured_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @staticmethod
    def hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class DomSnapshot:
    """DOM accessibility tree distilled (Stagehand / browser-use style).

    NO es HTML raw — es el arbol enumerado con refs estables `[1]`, `[2]`
    que el LLM usa para razonar. Tamaño tipico: 5-30 KB texto.
    """

    snapshot_id: UUID
    step_id: UUID
    moment: Literal["pre", "post"]
    content_hash: str
    char_count: int
    storage_uri: str = ""
    captured_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ScreenshotDiff:
    """Resultado de comparar dos screenshots.

    Heuristicas posibles:
      - hash igual          -> diferencia 0 (deduplicacion).
      - perceptual hash     -> distancia humana percibida.
      - pixel diff          -> ratio de pixeles que cambiaron.

    `change_ratio` en [0, 1]. Si excede `drift_threshold` de la vertical
    -> drift detectado, escalar o re-aprender selector.
    """

    pre_ref: str
    post_ref: str
    change_ratio: float
    pixel_diff_count: int = 0
    perceptual_distance: float = 0.0

    @property
    def is_significant(self) -> bool:
        return self.change_ratio > 0.05  # noqa: PLR2004  (5% threshold default)
