"""ScreenCaptureService — orquesta mutter source + GStreamer capture.

API estable para los consumidores (live view, training). Mantiene una sola
sesión de captura activa; multiplexar a varios suscriptores es responsabilidad
del consumidor (un fan-out de frames).
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from .domain import CaptureTarget, Frame, FrameCallback

logger = logging.getLogger(__name__)


class ScreenCaptureBackend(Protocol):
    """Permite sustituir mutter+gst por un fake en tests."""

    def start(self, target: CaptureTarget, on_frame: FrameCallback) -> None: ...
    def latest_frame(self) -> Frame | None: ...
    def stop(self) -> None: ...


class MutterGstBackend:
    """Backend real: MutterScreenCastSource + GstFrameCapture."""

    def __init__(self, *, fps: int = 15) -> None:
        self._fps = fps
        self._source = None
        self._capture = None

    def start(self, target: CaptureTarget, on_frame: FrameCallback) -> None:
        # Import perezoso: gi/Gst solo existen en el nodo gráfico real.
        from .gst_capture import GstFrameCapture
        from .mutter_source import MutterScreenCastSource

        self._source = MutterScreenCastSource()
        node_id = self._source.start(target)
        self._capture = GstFrameCapture(node_id, fps=self._fps)
        self._capture.start(on_frame)

    def latest_frame(self) -> Frame | None:
        return self._capture.latest_frame() if self._capture else None

    def stop(self) -> None:
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        if self._source is not None:
            self._source.stop()
            self._source = None


class ScreenCaptureService:
    """Punto único de control de la captura del compositor."""

    def __init__(self, backend: ScreenCaptureBackend | None = None) -> None:
        self._backend = backend or MutterGstBackend()
        self._active = False
        self._lock = threading.Lock()
        self._subscribers: list[FrameCallback] = []

    @property
    def is_active(self) -> bool:
        return self._active

    def subscribe(self, callback: FrameCallback) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: FrameCallback) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def _fan_out(self, frame: Frame) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(frame)
            except Exception:  # noqa: BLE001
                logger.exception("subscriber raised")

    def start(self, target: CaptureTarget) -> None:
        if self._active:
            return
        self._backend.start(target, self._fan_out)
        self._active = True
        logger.info("ScreenCaptureService started target=%s", target)

    def latest_frame(self) -> Frame | None:
        return self._backend.latest_frame()

    def stop(self) -> None:
        if not self._active:
            return
        self._backend.stop()
        self._active = False
        logger.info("ScreenCaptureService stopped")
