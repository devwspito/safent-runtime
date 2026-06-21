"""FramebufferCaptureAdapter — ScreenCaptureBackend via QQuickWindow.grabWindow().

Implements the ScreenCaptureBackend Protocol so SessionInputBridge can take
screenshots of the pocket compositor's framebuffer without any changes to the
bridge security layer.

grabWindow() is synchronous and must run on the GUI thread.  The adapter
marshals the capture to the GUI thread via QMetaObject.invokeMethod and returns
the result synchronously to the caller thread via a threading.Event.

Note: QQuickWindow.grabWindow() requires the window to be exposed (visible on
screen).  On the RK3588 with eglfs_kms this means the KMS/DRM surface must
be acquired.  In headless/offscreen dev mode it works with the offscreen QPA.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from PySide6.QtQuick import QQuickWindow

from hermes.shell_server.screen_capture.domain import CaptureTarget, Frame, FrameCallback

logger = logging.getLogger(__name__)

_GRAB_TIMEOUT_S: float = 5.0


class FramebufferCaptureAdapter:
    """Single-shot screenshot via QQuickWindow framebuffer grab.

    Args:
        window: The compositor's root QQuickWindow.  Pass None for offline mode.
    """

    def __init__(self, window: "QQuickWindow | None" = None) -> None:
        self._window = window
        self._latest: Frame | None = None
        self._callback: FrameCallback | None = None

    def start(self, target: CaptureTarget, on_frame: FrameCallback) -> None:
        """Grab one frame from the window and deliver it via on_frame.

        The grab is synchronous from the caller's perspective: this method
        blocks until the GUI thread has completed the grab or the timeout
        fires.
        """
        self._callback = on_frame
        self._latest = None

        if self._window is None:
            logger.warning("framebuffer_capture.no_window")
            return

        done = threading.Event()

        def _grab_on_gui() -> None:
            try:
                image = self._window.grabWindow()
                frame = self._qimage_to_frame(image)
                if frame is not None:
                    self._latest = frame
                    on_frame(frame)
            except Exception:
                logger.exception("framebuffer_capture.grab_failed")
            finally:
                done.set()

        # Marshal to GUI thread.
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(
            self._window,
            "grabWindowAndDeliver",
            Qt.ConnectionType.QueuedConnection,
        )
        # TODO(H0-HARDWARE): on RK3588 with eglfs_kms, grabWindow() may need
        # to be called after the next frame is rendered (connect to
        # QQuickWindow.afterRendering signal) to avoid a blank capture.
        # For now, direct invokeMethod works in offscreen/wayland dev mode.
        done.wait(timeout=_GRAB_TIMEOUT_S)

    def latest_frame(self) -> Frame | None:
        return self._latest

    def stop(self) -> None:
        """No-op: single-shot adapter has no ongoing resources to release."""
        self._callback = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _qimage_to_frame(image) -> Frame | None:
        """Convert a QImage to a raw RGBA Frame."""
        if image is None or image.isNull():
            return None

        from PySide6.QtGui import QImage

        # Ensure RGBA8888 for consistent stride/format expectations.
        rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
        width = rgba.width()
        height = rgba.height()
        bits = rgba.bits()
        # QImage.bits() returns a memoryview; copy to bytes for immutability.
        data = bytes(bits)

        return Frame(width=width, height=height, data=data, sequence=0)
