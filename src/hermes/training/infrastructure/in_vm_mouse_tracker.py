"""InVmMouseTracker — captura mouse track en Chromium handlers (T093).

IMPORTANTE (NFR-001a): la captura ocurre DENTRO de la VM, en los handlers
de eventos DOM de Chromium — NUNCA en el cliente del formador.

Esto garantiza que el track refleja el gesto humano sin distorsión por
latencia de red entre formador y VM.

Formato del blob:
  - Lista de puntos (x, y, ts_ms_offset) serializada en JSON comprimido (gzip).
  - Truncado a 30s de eventos DOM.
  - Downsample a 50 puntos/segundo (máx 1500 puntos por intervalo de 30s).
  - Detecta hesitations (pausas > 200ms) marcadas con flag `h=1`.
  - Detecta curvas para anti-detection learning.

El blob resultante es ephemeral: se usa para compilar la SkillPackage y se
descarta en el siguiente ciclo de GC (no persiste en almacenamiento externo).
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_MAX_WINDOW_SECONDS = 30
_MAX_POINTS_PER_SECOND = 50
_HESITATION_THRESHOLD_MS = 200
_MAX_BLOB_BYTES = 64 * 1024  # 64 KB cap


@dataclass(frozen=True, slots=True)
class MousePoint:
    x: float
    y: float
    ts_ms: int  # ms desde inicio de captura
    hesitation: bool = False


class InVmMouseTracker:
    """Acumula y procesa eventos de mouse capturados vía DOM handlers en la VM."""

    def __init__(self) -> None:
        self._points: list[MousePoint] = []
        self._capture_start_ms: int | None = None

    def record_event(self, x: float, y: float, ts_ms: int) -> None:
        """Registra un evento de mouse desde el DOM handler de Chromium."""
        if self._capture_start_ms is None:
            self._capture_start_ms = ts_ms

        elapsed_ms = ts_ms - self._capture_start_ms
        if elapsed_ms > _MAX_WINDOW_SECONDS * 1000:
            return

        self._points.append(MousePoint(x=x, y=y, ts_ms=elapsed_ms))

    def build_blob(self) -> bytes:
        """Construye el blob comprimido del mouse track.

        Pasos:
        1. Downsample a MAX_POINTS_PER_SECOND.
        2. Detecta hesitations (pausas > 200ms entre puntos).
        3. Serializa a JSON y comprime con gzip.
        4. Trunca si supera _MAX_BLOB_BYTES.
        """
        if not self._points:
            return b""

        downsampled = _downsample(self._points, _MAX_POINTS_PER_SECOND)
        annotated = _annotate_hesitations(downsampled, _HESITATION_THRESHOLD_MS)

        payload = [
            {"x": p.x, "y": p.y, "t": p.ts_ms, "h": 1 if p.hesitation else 0}
            for p in annotated
        ]
        raw = json.dumps(payload, separators=(",", ":")).encode()
        compressed = gzip.compress(raw, compresslevel=6)

        if len(compressed) > _MAX_BLOB_BYTES:
            compressed = _truncate_to_limit(raw, _MAX_BLOB_BYTES)
            logger.warning(
                "mouse_track_blob_truncated",
                extra={"original_bytes": len(raw), "limit_bytes": _MAX_BLOB_BYTES},
            )

        return compressed

    def reset(self) -> None:
        self._points = []
        self._capture_start_ms = None


def _downsample(points: list[MousePoint], max_per_second: int) -> list[MousePoint]:
    """Reduce la densidad de puntos a max_per_second conservando los extremos."""
    if not points:
        return []

    window_ms = 1000 // max_per_second
    result: list[MousePoint] = []
    last_kept_ms: int | None = None

    for p in points:
        if last_kept_ms is None or (p.ts_ms - last_kept_ms) >= window_ms:
            result.append(p)
            last_kept_ms = p.ts_ms

    return result


def _annotate_hesitations(
    points: list[MousePoint], threshold_ms: int
) -> list[MousePoint]:
    """Marca con hesitation=True los puntos precedidos de una pausa > threshold_ms."""
    if len(points) < 2:
        return list(points)

    result: list[MousePoint] = [points[0]]
    for prev, curr in zip(points, points[1:]):
        gap = curr.ts_ms - prev.ts_ms
        hesitation = gap > threshold_ms
        result.append(MousePoint(x=curr.x, y=curr.y, ts_ms=curr.ts_ms, hesitation=hesitation))

    return result


def _truncate_to_limit(raw: bytes, limit_bytes: int) -> bytes:
    """Trunca el payload JSON a N puntos hasta que el gzip quepa en limit_bytes."""
    data = json.loads(raw)
    while len(data) > 1:
        data = data[: len(data) * 3 // 4]
        compressed = gzip.compress(
            json.dumps(data, separators=(",", ":")).encode(), compresslevel=6
        )
        if len(compressed) <= limit_bytes:
            return compressed
    return gzip.compress(
        json.dumps(data[:1], separators=(",", ":")).encode(), compresslevel=6
    )
