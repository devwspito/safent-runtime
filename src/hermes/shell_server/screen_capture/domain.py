"""Dominio puro de captura de pantalla — sin GStreamer ni D-Bus."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable


class CaptureError(RuntimeError):
    """Fallo de captura (compositor no disponible, node perdido, etc.)."""


class CaptureTargetKind(StrEnum):
    MONITOR = "monitor"
    WINDOW = "window"


@dataclass(frozen=True, slots=True)
class CaptureTarget:
    """Qué capturar. MONITOR usa el connector (p.ej. 'Virtual-1').

    WINDOW captura una ventana concreta del compositor (mutter RecordWindow);
    sirve para enfocar una app de escritorio sin el resto del desktop.
    """

    kind: CaptureTargetKind
    monitor_connector: str | None = None
    window_id: int | None = None

    @staticmethod
    def monitor(connector: str) -> CaptureTarget:
        return CaptureTarget(
            kind=CaptureTargetKind.MONITOR, monitor_connector=connector
        )

    @staticmethod
    def window(window_id: int) -> CaptureTarget:
        return CaptureTarget(kind=CaptureTargetKind.WINDOW, window_id=window_id)


@dataclass(frozen=True, slots=True)
class Frame:
    """Un frame RGBA crudo del compositor.

    `data` es bytes RGBA contiguos de tamaño width*height*4 (stride = width*4),
    listos para `Gdk.MemoryTexture` (formato R8G8B8A8) o para escribir un PNG.
    """

    width: int
    height: int
    data: bytes
    sequence: int

    @property
    def stride(self) -> int:
        return self.width * 4

    def is_blank(self) -> bool:
        """True si todos los bytes son cero (compositor sin render aún)."""
        return not any(self.data[: min(len(self.data), 65536)])


# Callback que recibe cada frame nuevo. Debe ser barato / no bloqueante;
# el consumidor (UI) reenvía a su loop con idle_add.
FrameCallback = Callable[[Frame], None]
