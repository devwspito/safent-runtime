"""Shared helpers for the jailed browser's live-view WebSocket bridges.

Split out of the retired teach-by-browser feature's ``training_live.py``
(retired 10-sep-2026, see specs/025-safent-repaso/retirada-ensenar.md): these
are generic CDP/playwright/token plumbing with zero coupling to teaching.
``vnc_proxy.py``, ``watch_live.py``, ``clipboard_bridge.py`` and
``system_update.py`` depend on them to keep the read-only "En vivo" view
(and its token-gated siblings) working.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("hermes.shell_server.cowork.live_view_support")

# CDP URL: use env override or fall back to the fixed veth address.
_DEFAULT_CDP_URL = "http://10.200.0.2:9333"

# Frame-send poll interval. Tight so a new capture reaches the client promptly (the
# screenshot/screencast source's capture rate is the real fps limiter, not this). We
# only send when a NEW frame exists (frame-count change), so a static page costs no
# WS traffic.
_FRAME_INTERVAL_S: float = 0.012


def cdp_url() -> str:
    return os.environ.get("BROWSER_CDP_URL", _DEFAULT_CDP_URL)


def verify_token(candidate: str, expected: str) -> bool:
    """Constant-time token comparison (CWE-208)."""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)


async def try_ensure_browser_running(session_name: str = "exec-browse") -> None:
    """Call JailedBrowserManager.ensure_running() best-effort; never raises.

    session_name defaults to the shared agent-execution session; callers that
    watch a specific session (e.g. vnc_proxy) pass the one they were asked to view.
    """
    try:
        from hermes.runtime.jailed_browser_manager import (  # noqa: PLC0415
            JailedBrowserManager,
        )

        mgr = JailedBrowserManager(session_name=session_name)
        await asyncio.wait_for(mgr.ensure_running(), timeout=30.0)
    except Exception:  # noqa: BLE001
        logger.debug("hermes.live_view_support.ensure_running.skipped", exc_info=True)


async def send_frames(ws: WebSocket, src) -> None:
    """Forward each NEW JPEG frame to the client promptly.

    Dedups by frame count so a static page costs no WS traffic; the source's
    capture rate is the fps limiter. Works with any source exposing
    ``.latest() -> (bytes | None, ...)`` and ``.frame_count`` (screenshot- and
    screencast-backed sources both qualify).
    """
    last_count = -1
    while True:
        data, _ = src.latest()
        count = src.frame_count
        if data is not None and count != last_count:
            last_count = count
            try:
                await ws.send_bytes(data)
            except (WebSocketDisconnect, RuntimeError):
                return
        await asyncio.sleep(_FRAME_INTERVAL_S)


async def stop_playwright_safe(pw) -> None:
    try:
        await pw.stop()
    except Exception:  # noqa: BLE001
        logger.debug("hermes.live_view_support.pw_stop_error", exc_info=True)
