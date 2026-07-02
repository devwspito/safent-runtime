"""CdpScreencastSource — FrameSourcePort backed by CDP Page.startScreencast.

Attaches a CDP session to a Playwright Page already connected to the jailed
Chromium and starts `Page.startScreencast`. Each `Page.screencastFrame` event
carries a base64-encoded JPEG; we decode and store the latest frame so that
`MirrorServer` can poll it at its own rate via `latest()`.

Seam B (spec 012 §Control-por-accion-live-view): the live view of the sandbox
browser is produced here instead of via mutter/GStreamer, because the sandbox
Chromium window is not visible to the host Wayland compositor.

Thread-safety: `latest()` is called from the asyncio event loop thread that
runs MirrorServer._send_frames. `_on_frame` is also called from the same loop
(Playwright event handlers run in the event loop). Both access `_latest` and
`_size` under `_lock` for safety if the loop is ever moved to a thread pool.

Lifecycle:
    source = CdpScreencastSource(page=playwright_page, quality=60)
    await source.start()
    # ... MirrorServer polls source.latest()
    source.stop()          # synchronous; can be called from __main__ cleanup

The `Page.screencastFrameAck` must be sent for every received frame; otherwise
Chrome stops delivering frames. We fire-and-forget the ack from the event
handler using `asyncio.ensure_future`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import CDPSession, Page

logger = logging.getLogger(__name__)

# JPEG quality and cap. Raised from 60/1280x720: on a large / Retina display the
# old low-res, low-quality frame was upscaled into an illegible blur. 82 quality
# at 1600x900 (matched to the teaching context viewport, deviceScaleFactor=1 so
# the frame's device pixels == the CSS viewport → operator click coordinates stay
# 1:1) is crisp without an unreasonable bandwidth cost on localhost.
_DEFAULT_QUALITY: int = 82
_DEFAULT_MAX_WIDTH: int = 1600
_DEFAULT_MAX_HEIGHT: int = 900

_SCREENCAST_PARAMS: dict[str, Any] = {
    "format": "jpeg",
    "everyNthFrame": 1,
}


class CdpScreencastSourceError(RuntimeError):
    """Raised when the CDP screencast cannot start or loses the session."""


class CdpScreencastSource:
    """Frame source that receives JPEG frames from CDP Page.startScreencast.

    Implements FrameSourcePort (structural, no explicit Protocol import here
    to avoid a circular dependency — the Protocol lives in shell_server/mirror).

    Args:
        page:     Playwright Page object connected to the jailed Chromium.
        quality:  JPEG quality (1-100).
        max_width:  Maximum screencast width in logical pixels.
        max_height: Maximum screencast height in logical pixels.
    """

    def __init__(
        self,
        *,
        page: "Page",
        quality: int = _DEFAULT_QUALITY,
        max_width: int = _DEFAULT_MAX_WIDTH,
        max_height: int = _DEFAULT_MAX_HEIGHT,
    ) -> None:
        self._page = page
        self._quality = quality
        self._max_width = max_width
        self._max_height = max_height

        self._session: "CDPSession | None" = None
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._size: tuple[int, int] = (0, 0)
        self._frame_count: int = 0
        self._running = False

    # ------------------------------------------------------------------
    # FrameSourcePort implementation
    # ------------------------------------------------------------------

    def latest(self) -> tuple[bytes | None, tuple[int, int]]:
        """Return the most recent JPEG frame and its (width, height)."""
        with self._lock:
            return self._latest, self._size

    def stop(self) -> None:
        """Stop the screencast and detach the CDP session. Idempotent."""
        if not self._running:
            return
        self._running = False
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(self._async_stop())
        else:
            loop.run_until_complete(self._async_stop())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Attach a CDP session and start the screencast.

        Raises:
            CdpScreencastSourceError: if the session cannot be created.
        """
        if self._running:
            return

        try:
            self._session = await self._page.context.new_cdp_session(self._page)
        except Exception as exc:
            raise CdpScreencastSourceError(
                f"Cannot create CDP session for screencast: {exc}"
            ) from exc

        self._session.on("Page.screencastFrame", self._handle_screencast_frame)

        await self._session.send(
            "Page.startScreencast",
            {
                **_SCREENCAST_PARAMS,
                "quality": self._quality,
                "maxWidth": self._max_width,
                "maxHeight": self._max_height,
            },
        )
        self._running = True
        logger.info(
            "hermes.cdp_screencast_source.started quality=%d max=%dx%d",
            self._quality, self._max_width, self._max_height,
        )

    async def _async_stop(self) -> None:
        if self._session is None:
            return
        try:
            await self._session.send("Page.stopScreencast")
        except Exception:  # noqa: BLE001 — session may already be gone
            logger.debug("cdp_screencast_source: stopScreencast ignored", exc_info=True)
        try:
            await self._session.detach()
        except Exception:  # noqa: BLE001
            logger.debug("cdp_screencast_source: detach ignored", exc_info=True)
        self._session = None
        logger.info(
            "hermes.cdp_screencast_source.stopped frames_total=%d",
            self._frame_count,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_screencast_frame(self, params: dict[str, Any]) -> None:
        """CDP event handler — runs in the asyncio event loop thread."""
        raw_b64: str = params.get("data", "")
        session_id: int = params.get("sessionId", 0)
        metadata: dict = params.get("metadata", {})

        if not raw_b64:
            return

        try:
            jpeg_bytes = base64.b64decode(raw_b64)
        except Exception:  # noqa: BLE001
            logger.warning("cdp_screencast_source: b64 decode failed")
            return

        w = int(metadata.get("deviceWidth", 0))
        h = int(metadata.get("deviceHeight", 0))

        with self._lock:
            self._latest = jpeg_bytes
            self._size = (w, h)
            self._frame_count += 1

        logger.debug(
            "hermes.cdp_screencast_source.frame bytes=%d size=%dx%d count=%d",
            len(jpeg_bytes), w, h, self._frame_count,
        )

        # Ack is mandatory: Chrome stops delivering frames without it.
        # Guard: only schedule when a loop is actually running (no-op in unit tests
        # that call this handler synchronously without an event loop).
        if self._session is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            asyncio.ensure_future(
                self._session.send(
                    "Page.screencastFrameAck", {"sessionId": session_id}
                ),
                loop=loop,
            )

    @property
    def frame_count(self) -> int:
        """Total frames received since start(). Thread-safe."""
        with self._lock:
            return self._frame_count
