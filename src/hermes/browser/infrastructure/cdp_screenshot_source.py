"""CdpScreenshotSource — FrameSourcePort backed by CDP Page.captureScreenshot.

Why not Page.startScreencast: GROUND-TRUTH MEASURED (2026-07-02, real jailed
Chromium) — startScreencast captures at the page LAYOUT VIEWPORT resolution and
IGNORES deviceScaleFactor at every level (per-context, per-session Emulation, AND
the compositor --force-device-scale-factor flag). So a screencast frame is always
1×viewport → blurry when the client draws it into a CSS×dpr (Retina) canvas.

Page.captureScreenshot, by contrast, DOES honour deviceScaleFactor (measured: a
1520×850 CSS viewport at deviceScaleFactor=2 → a 3040×1700 screenshot). So we drive
a poll loop that:
  1. sets Emulation.setDeviceMetricsOverride(width=CSS, height=CSS, dsf=client-dpr)
     → the page LAYS OUT at the display's CSS size (native-looking proportions) …
  2. … and captureScreenshot returns it at CSS×dpr device pixels (crisp on Retina).
The client paints that into its CSS×dpr canvas backing store 1:1 = sharp AND
correctly dimensioned — like the native browser. Measured throughput ~12 fps at
3040×1700 (~30 KB/frame) on localhost, on par with the old screencast.

Interface matches CdpScreencastSource (latest()/start()/stop()) so it is a drop-in
FrameSourcePort; adds set_metrics(w_css, h_css, dpr) for live resize.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import CDPSession, Page

logger = logging.getLogger(__name__)

# JPEG quality: captureScreenshot at 2× is already crisp; 70 keeps frames ~30 KB.
_DEFAULT_QUALITY: int = 70
# Poll pacing. capture is the natural rate-limiter (surface readback ~fixed cost),
# so we keep the extra sleep tiny → capture back-to-back for MAX fps (fluidity).
# On a fast host (localhost, Apple Silicon) this streams well above the old 14 fps
# screencast; on a slow/loaded box it floors around ~12 fps. Short teaching sessions
# make the idle-CPU cost of near-continuous capture acceptable.
_POLL_INTERVAL_S: float = 0.004
# Bounds. CSS viewport (device px = these × dpr, ≤ ~5120×3200 at dpr 2).
_MIN_W, _MIN_H = 320, 240
_MAX_W, _MAX_H = 2560, 1600
_MIN_DPR, _MAX_DPR = 1.0, 3.0


class CdpScreenshotSourceError(RuntimeError):
    """Raised when the CDP screenshot source cannot start."""


class CdpScreenshotSource:
    """Frame source that polls Page.captureScreenshot at the client's dpr.

    Args:
        page:              Playwright Page connected to the jailed Chromium.
        quality:           JPEG quality (1-100).
        initial_width:     initial CSS viewport width (until the client resizes).
        initial_height:    initial CSS viewport height.
        device_scale_factor: initial dpr (the client overrides it on resize).
    """

    def __init__(
        self,
        *,
        page: "Page",
        quality: int = _DEFAULT_QUALITY,
        initial_width: int = 1280,
        initial_height: int = 720,
        device_scale_factor: float = 2.0,
    ) -> None:
        self._page = page
        self._quality = quality
        self._w = _clamp(initial_width, _MIN_W, _MAX_W)
        self._h = _clamp(initial_height, _MIN_H, _MAX_H)
        self._dsf = _clamp(device_scale_factor, _MIN_DPR, _MAX_DPR)

        self._session: "CDPSession | None" = None
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._size: tuple[int, int] = (0, 0)
        self._frame_count: int = 0
        self._running = False
        self._metrics_dirty = True
        self._poll_task: "asyncio.Task | None" = None

    # ------------------------------------------------------------------
    # FrameSourcePort implementation
    # ------------------------------------------------------------------

    def latest(self) -> tuple[bytes | None, tuple[int, int]]:
        """Return the most recent JPEG frame and its (width, height) in device px."""
        with self._lock:
            return self._latest, self._size

    def stop(self) -> None:
        """Stop the poll loop and clear the metrics override. Idempotent."""
        if not self._running:
            return
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if loop.is_running():
            asyncio.ensure_future(self._async_stop())

    # ------------------------------------------------------------------
    # Live resize
    # ------------------------------------------------------------------

    def set_metrics(self, w_css: int, h_css: int, dpr: float) -> None:
        """Update the CSS viewport + dpr. Applied on the next poll iteration.

        Called from the event-loop thread (the WS resize handler).
        """
        w = _clamp(int(w_css), _MIN_W, _MAX_W)
        h = _clamp(int(h_css), _MIN_H, _MAX_H)
        d = _clamp(float(dpr), _MIN_DPR, _MAX_DPR)
        with self._lock:
            if (w, h, d) == (self._w, self._h, self._dsf):
                return
            self._w, self._h, self._dsf = w, h, d
            self._metrics_dirty = True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        try:
            self._session = await self._page.context.new_cdp_session(self._page)
        except Exception as exc:  # noqa: BLE001
            raise CdpScreenshotSourceError(
                f"Cannot create CDP session for screenshot source: {exc}"
            ) from exc
        await self._apply_metrics()
        self._running = True
        self._poll_task = asyncio.ensure_future(self._poll_loop())
        logger.info(
            "hermes.cdp_screenshot_source.started quality=%d css=%dx%d dpr=%.1f",
            self._quality, self._w, self._h, self._dsf,
        )

    async def _async_stop(self) -> None:
        if self._session is None:
            return
        try:
            await self._session.send("Emulation.clearDeviceMetricsOverride")
        except Exception:  # noqa: BLE001
            logger.debug("cdp_screenshot_source: clear metrics ignored", exc_info=True)
        try:
            await self._session.detach()
        except Exception:  # noqa: BLE001
            logger.debug("cdp_screenshot_source: detach ignored", exc_info=True)
        self._session = None
        logger.info(
            "hermes.cdp_screenshot_source.stopped frames_total=%d", self._frame_count
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _apply_metrics(self) -> None:
        if self._session is None:
            return
        with self._lock:
            w, h, dsf = self._w, self._h, self._dsf
            self._metrics_dirty = False
        await self._session.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": w,
                "height": h,
                "deviceScaleFactor": dsf,
                "mobile": False,
                "screenWidth": w,
                "screenHeight": h,
            },
        )

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                if self._metrics_dirty:
                    await self._apply_metrics()
                resp = await self._session.send(  # type: ignore[union-attr]
                    "Page.captureScreenshot",
                    {
                        "format": "jpeg",
                        "quality": self._quality,
                        "captureBeyondViewport": False,
                        "fromSurface": True,
                    },
                )
                jpeg = base64.b64decode(resp["data"])
                with self._lock:
                    self._latest = jpeg
                    self._size = (round(self._w * self._dsf), round(self._h * self._dsf))
                    self._frame_count += 1
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 — transient (nav in flight) or session gone
                logger.debug("cdp_screenshot_source: capture skipped", exc_info=True)
                await asyncio.sleep(0.2)
            await asyncio.sleep(_POLL_INTERVAL_S)

    @property
    def frame_count(self) -> int:
        with self._lock:
            return self._frame_count


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi))
