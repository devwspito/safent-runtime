"""FrameSourcePort — protocol for frame sources consumed by MirrorServer.

Both JpegFrameSource (mutter/GStreamer pipeline) and CdpScreencastSource
(CDP Page.startScreencast) implement this protocol so that MirrorServer
accepts either without a mutter dependency.

The protocol is intentionally minimal: callers only need the latest stored
frame; push vs. pull scheduling is internal to each implementation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class FrameSourcePort(Protocol):
    """Source of JPEG frames pushed into the mirror WebSocket server.

    Implementations guarantee:
    - latest() is safe to call from any thread (lock-protected internally).
    - latest() returns (None, (0, 0)) when no frame has arrived yet.
    - stop() is idempotent and releases all async resources.
    """

    def latest(self) -> tuple[bytes | None, tuple[int, int]]:
        """Return the most recent JPEG frame bytes and (width, height).

        Returns (None, (0, 0)) if no frame has been received yet.
        """
        ...

    def stop(self) -> None:
        """Release resources held by this source. Idempotent."""
        ...
