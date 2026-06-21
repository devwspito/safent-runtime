"""FakeScreenCaptureBackend — frames deterministas sin compositor (CI)."""

from __future__ import annotations

from .domain import CaptureTarget, Frame, FrameCallback


def _checker(width: int, height: int, seq: int) -> bytes:
    """RGBA con un patrón que varía por seq (no-blanco, verificable)."""
    buf = bytearray(width * height * 4)
    for i in range(0, len(buf), 4):
        v = (i // 4 + seq) % 256
        buf[i] = v
        buf[i + 1] = (v * 2) % 256
        buf[i + 2] = (v * 3) % 256
        buf[i + 3] = 255
    return bytes(buf)


class FakeScreenCaptureBackend:
    """Genera N frames sintéticos al arrancar; útil para tests del service."""

    def __init__(self, *, width: int = 8, height: int = 8, frames: int = 3) -> None:
        self._w = width
        self._h = height
        self._frames = frames
        self._latest: Frame | None = None
        self._started = False

    def start(self, target: CaptureTarget, on_frame: FrameCallback) -> None:
        self._started = True
        for seq in range(1, self._frames + 1):
            frame = Frame(
                width=self._w,
                height=self._h,
                data=_checker(self._w, self._h, seq),
                sequence=seq,
            )
            self._latest = frame
            on_frame(frame)

    def latest_frame(self) -> Frame | None:
        return self._latest

    def stop(self) -> None:
        self._started = False
