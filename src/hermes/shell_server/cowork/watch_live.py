"""Read-only WebSocket live-view of the AGENT's internal jailed browser.

Route: WS /api/v1/watch/agent/live

Purpose ("Verificar")
---------------------
The operator asks the agent (via chat) to run a task and WATCHES it execute in
real time. This view is **read-only**: it screencasts the page the AGENT is
actively using in the shared jailed Chromium so the human can corroborate the
run — it does NOT inject input and does NOT create a context, it attaches to
the agent's existing page.

Shares its CDP endpoint, `CdpScreencastSource`, JPEG-over-WS frame loop, and
token auth with the other live-view bridges via `live_view_support.py`.

Page selection (best-effort)
----------------------------
The jailed Chromium is a single shared headless instance; there is no task→page
registry (a robust binding would need a daemon verb). We poll the open pages and
attach to the most-recently-created page with a real (non-blank) URL, waiting for
the agent to start browsing. Same-page navigations are followed automatically
(the screencast is bound to the page); opening a brand-new tab is not followed in
this version.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from hermes.browser.infrastructure.cdp_screencast_source import CdpScreencastSource
from hermes.shell_server.cowork.live_view_support import (
    cdp_url,
    send_frames,
    stop_playwright_safe,
    try_ensure_browser_running,
)
from hermes.shell_server.main import authenticate_websocket

logger = logging.getLogger("hermes.shell_server.cowork.watch_live")

# How long to wait for the agent to open a real page before giving up.
_PAGE_WAIT_TIMEOUT_S: float = 45.0
_PAGE_POLL_INTERVAL_S: float = 1.0


def _is_real_url(url: str) -> bool:
    return bool(url) and not url.startswith(("about:", "chrome:", "chrome-extension:", "devtools:"))


def _pick_agent_page(browser):
    """Return the most-relevant open page in the shared jailed browser, or None.

    Prefers the last (most-recently-created) page with a real URL; falls back to
    the last open page overall (so the operator at least sees the browser).
    """
    real: list = []
    any_open: list = []
    for ctx in browser.contexts:
        for pg in ctx.pages:
            try:
                if pg.is_closed():
                    continue
                any_open.append(pg)
                if _is_real_url(pg.url):
                    real.append(pg)
            except Exception:  # noqa: BLE001 — page may be tearing down
                continue
    if real:
        return real[-1]
    if any_open:
        return any_open[-1]
    return None


async def _wait_for_agent_page(browser):
    """Poll until the agent opens a real page, or the timeout elapses.

    Returns (page_with_real_url) as soon as one exists; after the timeout returns
    whatever open page there is (possibly blank) or None if the browser has none.
    """
    waited = 0.0
    while True:
        page = _pick_agent_page(browser)
        if page is not None and _is_real_url(getattr(page, "url", "")):
            return page
        if waited >= _PAGE_WAIT_TIMEOUT_S:
            return page  # blank page or None — best effort
        await asyncio.sleep(_PAGE_POLL_INTERVAL_S)
        waited += _PAGE_POLL_INTERVAL_S


def create_watch_live_router() -> APIRouter:
    router = APIRouter()

    @router.websocket("/api/v1/watch/agent/live")
    async def watch_agent_live(websocket: WebSocket) -> None:
        # Auth: WebSocket upgrades never reach the HTTP `_require_operator_token`
        # middleware (Starlette only runs @app.middleware("http") for scope["type"]
        # == "http") — `authenticate_websocket` is the shared per-connection gate
        # (same bearer check, closes 1008 before accept on failure).
        if not await authenticate_websocket(websocket):
            return

        await websocket.accept()
        logger.info("hermes.watch_live.session.start remote=%s", websocket.client)

        pw = None
        browser = None
        screen_src = None
        send_task = None
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415

            await try_ensure_browser_running()
            pw = await async_playwright().start()
            browser = await pw.chromium.connect_over_cdp(cdp_url())

            page = await _wait_for_agent_page(browser)
            if page is None:
                await websocket.send_json({
                    "type": "status",
                    "message": "El agente aún no ha abierto el navegador.",
                })
                await websocket.close(code=1000, reason="no agent page")
                return

            screen_src = CdpScreencastSource(page=page)
            await screen_src.start()

            send_task = asyncio.create_task(send_frames(websocket, screen_src))
            await send_task

        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("hermes.watch_live.session.error")
        finally:
            if send_task and not send_task.done():
                send_task.cancel()
            if screen_src is not None:
                screen_src.stop()
            # We attach to the AGENT's page — never close its context/page here.
            if pw is not None:
                await stop_playwright_safe(pw)
            logger.info("hermes.watch_live.session.end")

    return router
