"""WebSocket live-view for the LIVE browser teaching feature.

Route: WS /api/v1/training/{session_id}/live

Auth
----
WebSocket upgrades are plain GET requests, so the HTTP operator-token
middleware (which only gates POST/PUT/PATCH/DELETE) does not run.  We
replicate the token check in-handler using the same stable webui bearer
that the React UI holds as ``window.__LUMEN_TOKEN__``.  The token is
passed as a ``?token=<value>`` query parameter (matching the pattern in
MirrorServer._authed_token).  Missing or invalid token → close 1008.

Wire
----
On connect:
  1. Best-effort ``JailedBrowserManager.ensure_running()`` so the CDP
     port is alive before we try to connect.
  2. ``connect_over_cdp(CDP_URL)`` with a *fresh* isolated context
     (no shared cookies / storage with the agent's sessions).
  3. ``CdpScreencastSource(page)`` receives frames via
     ``Page.startScreencast``; ``CdpInputAdapter(session)`` injects
     pointer / keyboard events.

Two concurrent tasks (cancelled on disconnect / error):
  - SEND: every ~70 ms, forward the latest JPEG frame as binary WS
    message.
  - RECV: JSON messages from the client → CdpInputAdapter calls or
    page.goto().

Input message contract (client → server JSON text)
---------------------------------------------------
  Mouse:     {"type":"mouse","action":"move","xf":<0..1>,"yf":<0..1>}
             {"type":"mouse","action":"down","xf":<0..1>,"yf":<0..1>,"button":<0|1|2>}
             {"type":"mouse","action":"up",  "xf":<0..1>,"yf":<0..1>,"button":<0|1|2>}
             xf/yf are NORMALIZED fractions of the frame (server maps to CSS px);
             legacy integer x/y are still accepted for non-web clients.
  Keyboard:  {"type":"key","action":"down","keysym":<int>}
             {"type":"key","action":"up",  "keysym":<int>}
             {"type":"key","action":"char","text":<str>}   (printable text insert)
  Navigate:  {"type":"navigate","url":<str>}

Button indices follow the web convention (0=left, 1=middle, 2=right) and
are mapped to the evdev codes that CdpInputAdapter.pointer_button expects
(BTN_LEFT=0x110, BTN_MIDDLE=0x112, BTN_RIGHT=0x111).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.browser.infrastructure.cdp_input_adapter import CdpInputAdapter
from hermes.browser.infrastructure.cdp_screencast_source import CdpScreencastSource
from hermes.shell_server.mirror.button_codes import BTN_LEFT, BTN_MIDDLE, BTN_RIGHT

logger = logging.getLogger("hermes.shell_server.cowork.training_live")

# CDP URL: use env override or fall back to the fixed veth address.
_DEFAULT_CDP_URL = "http://10.200.0.2:9333"

# Frame-send interval in seconds (~14 fps, same as MirrorServer).
_FRAME_INTERVAL_S: float = 0.07

# Map web button index → evdev code expected by CdpInputAdapter.pointer_button.
# Web: 0=left, 1=middle, 2=right.
# Evdev: BTN_LEFT=0x110(272), BTN_MIDDLE=0x112(274), BTN_RIGHT=0x111(273).
_BTN_MAP: dict[int, int] = {0: BTN_LEFT, 1: BTN_MIDDLE, 2: BTN_RIGHT}

# ADAPTIVE RESOLUTION. Measured truth (2026-07-02): the CDP screencast IGNORES
# deviceScaleFactor over connect_over_cdp — the frame resolution EQUALS the page
# VIEWPORT (DSF=2 vp1600x900 → frame 1600x900, blurry when upscaled). So we render
# at DSF=1 and set the viewport to the CLIENT's physical canvas size (CSS px ×
# devicePixelRatio), which it reports via {"type":"resize"}. set_viewport_size is
# honoured live → the frame follows (verified 1280x720 → 2880x1620). Clicks use
# NORMALIZED fractions mapped against the CURRENT viewport, so any size is exact.
_TEACH_VIEWPORT_W: int = 1600   # initial, until the client reports its real size
_TEACH_VIEWPORT_H: int = 900
_TEACH_MAX_W: int = 4096        # generous screencast cap; viewport (≤ this) drives it
_TEACH_MAX_H: int = 2160


def _cdp_url() -> str:
    return os.environ.get("BROWSER_CDP_URL", _DEFAULT_CDP_URL)


def _verify_token(candidate: str, expected: str) -> bool:
    """Constant-time token comparison (CWE-208)."""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)


def create_training_live_router(orchestrator=None) -> APIRouter:
    """orchestrator: shared TrainingSessionOrchestrator (DI from main) so the
    operator's demonstrated actions are captured as steps and /sign produces a
    non-empty skill. None → live-view only (no recording)."""
    router = APIRouter()

    @router.websocket("/api/v1/training/{session_id}/live")
    async def training_live(
        websocket: WebSocket,
        session_id: str,
    ) -> None:
        # --- Layer 1: token auth (replaces HTTP middleware, which is GET-exempt) ---
        # WebSocket routes get app.state via websocket.app.state (no Request param —
        # FastAPI does not inject Request into WS handlers).
        webui_token: str = getattr(websocket.app.state, "shell_webui_token", "")
        candidate_token: str = websocket.query_params.get("token", "")
        if not _verify_token(candidate_token, webui_token):
            logger.warning(
                "hermes.training_live.auth.bad_token",
                extra={"session_id": session_id, "remote": websocket.client},
            )
            await websocket.close(code=1008, reason="unauthorized")
            return

        await websocket.accept()
        logger.info(
            "hermes.training_live.session.start",
            extra={"session_id": session_id, "remote": websocket.client},
        )

        pw = None
        ctx = None
        screen_src = None
        send_task = None
        recv_task = None

        try:
            pw, ctx, page, screen_src, input_adapter = await _setup_browser_session()
            sid = _parse_uuid(session_id)
            # Mutable per-connection viewport (CSS px). Updated by the client's
            # {"type":"resize"} to its physical canvas size; used BOTH to
            # set_viewport_size (crisp frame) AND to map normalized clicks → CSS px.
            viewport = {"w": _TEACH_VIEWPORT_W, "h": _TEACH_VIEWPORT_H}
            send_task = asyncio.create_task(_send_frames(websocket, screen_src))
            recv_task = asyncio.create_task(
                _recv_input(websocket, input_adapter, page, orchestrator, sid, viewport)
            )

            # Wait until either task exits (disconnect or error).
            done, pending = await asyncio.wait(
                [send_task, recv_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            # Propagate any unexpected exception from completed tasks.
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    logger.warning(
                        "hermes.training_live.task_error",
                        extra={"session_id": session_id, "error": str(exc)},
                    )

        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception(
                "hermes.training_live.session.error",
                extra={"session_id": session_id},
            )
        finally:
            if send_task and not send_task.done():
                send_task.cancel()
            if recv_task and not recv_task.done():
                recv_task.cancel()
            if screen_src is not None:
                screen_src.stop()
            if ctx is not None:
                await _close_context_safe(ctx)
            if pw is not None:
                await _stop_playwright_safe(pw)
            logger.info(
                "hermes.training_live.session.end",
                extra={"session_id": session_id},
            )

    return router


# ---------------------------------------------------------------------------
# Browser session setup
# ---------------------------------------------------------------------------


async def _setup_browser_session():
    """Connect to the jailed CDP, open an isolated context, wire adapters.

    Returns (pw, ctx, page, CdpScreencastSource, CdpInputAdapter).
    Raises on hard failure (caller closes the WS).
    """
    from playwright.async_api import async_playwright  # noqa: PLC0415

    # Best-effort: ensure the jailed browser is running before connecting.
    await _try_ensure_browser_running()

    # Egress: the jailed browser's IP is DEFAULT_DENY at the proxy until a policy
    # is pushed (the agent does this on session open). Teaching is a human-supervised
    # LIVE session, so push "open-logged" (teaching_mode) — otherwise every navigate
    # dies with net::ERR_CONNECTION_CLOSED and the operator cannot demonstrate.
    try:
        from hermes.security.browser_jail import push_egress_policy  # noqa: PLC0415

        push_egress_policy(
            session_name="teaching-live",
            domains_whitelist=(),
            teaching_mode=True,
        )
    except Exception:  # noqa: BLE001 — no proxy socket in dev/CI → navigate direct
        logger.debug("hermes.training_live.egress_policy.skipped", exc_info=True)

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(_cdp_url())

    # Isolated context: no shared cookies/storage with the agent. DSF=1 (the
    # screencast ignores DSF); the initial viewport is a placeholder — the client
    # reports its real physical canvas size via {"type":"resize"} and we
    # set_viewport_size to match so the frame is 1:1 with the display (crisp at any
    # size / fullscreen). The screencast cap is generous; the viewport drives it.
    ctx = await browser.new_context(
        viewport={"width": _TEACH_VIEWPORT_W, "height": _TEACH_VIEWPORT_H},
        device_scale_factor=1,
    )
    page = await ctx.new_page()

    screen_src = CdpScreencastSource(
        page=page,
        max_width=_TEACH_MAX_W,
        max_height=_TEACH_MAX_H,
    )
    await screen_src.start()

    # Separate CDPSession for input injection (CdpScreencastSource owns its own).
    input_session = await ctx.new_cdp_session(page)
    input_adapter = CdpInputAdapter(session=input_session)

    return pw, ctx, page, screen_src, input_adapter


async def _try_ensure_browser_running() -> None:
    """Call JailedBrowserManager.ensure_running() best-effort; never raises."""
    try:
        from hermes.runtime.jailed_browser_manager import (  # noqa: PLC0415
            JailedBrowserManager,
        )

        mgr = JailedBrowserManager()
        await asyncio.wait_for(mgr.ensure_running(), timeout=30.0)
    except Exception:  # noqa: BLE001
        logger.debug(
            "hermes.training_live.ensure_running.skipped", exc_info=True
        )


# ---------------------------------------------------------------------------
# Frame sender
# ---------------------------------------------------------------------------


async def _send_frames(ws: WebSocket, src: CdpScreencastSource) -> None:
    """Forward the latest JPEG from the screencast to the client at ~14 fps."""
    while True:
        data, _ = src.latest()
        if data is not None:
            try:
                await ws.send_bytes(data)
            except (WebSocketDisconnect, RuntimeError):
                return
        await asyncio.sleep(_FRAME_INTERVAL_S)


# ---------------------------------------------------------------------------
# Input receiver
# ---------------------------------------------------------------------------


async def _recv_input(
    ws: WebSocket,
    adapter: CdpInputAdapter,
    page,  # playwright Page
    orchestrator=None,
    session_id: "UUID | None" = None,
    viewport: dict | None = None,
) -> None:
    """Receive JSON input events from the client, dispatch them to the browser,
    and (if recording) capture them as training steps."""
    vp = viewport if viewport is not None else {"w": _TEACH_VIEWPORT_W, "h": _TEACH_VIEWPORT_H}
    while True:
        try:
            raw = await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            return

        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            logger.debug("hermes.training_live.recv.bad_json raw=%r", raw)
            continue

        if ev.get("type") == "resize":
            _dispatch_resize(ev, page, vp)
            continue

        _normalize_mouse_coords(ev, vp)
        _dispatch_event(ev, adapter, page)
        # Semantic capture: resolve a click's coordinate to the actual element so the
        # recorded step is "click the Search button", not "click at (640,300)".
        if ev.get("type") == "mouse" and ev.get("action") == "down":
            try:
                el = await adapter.resolve_element_at(ev.get("x", 0), ev.get("y", 0))
                if el:
                    ev["element"] = el
            except Exception:  # noqa: BLE001 — fall back to coordinates
                pass
        _record_step(orchestrator, session_id, ev)


def _normalize_mouse_coords(ev: dict, viewport: dict) -> None:
    """Convert NORMALIZED client coords (xf/yf ∈ [0,1] of the frame) into CSS px
    of the CURRENT teaching viewport, in place, so all downstream code (dispatch,
    element resolution, step capture) keeps reading ev['x']/ev['y'] as CSS px. The
    client sends fractions so it never needs to know the source resolution/DSF; the
    server owns the (dynamic) viewport truth. Legacy pixel events pass through.
    """
    if ev.get("type") != "mouse":
        return
    xf, yf = ev.get("xf"), ev.get("yf")
    if xf is None or yf is None:
        return
    try:
        fx = min(1.0, max(0.0, float(xf)))
        fy = min(1.0, max(0.0, float(yf)))
    except (TypeError, ValueError):
        return
    ev["x"] = round(fx * viewport["w"])
    ev["y"] = round(fy * viewport["h"])


def _dispatch_resize(ev: dict, page, viewport: dict) -> None:
    """Client reported its physical canvas size → resize the teaching page so the
    screencast frame is 1:1 with the display (crisp at any size / fullscreen).
    Updates the shared viewport dict (used to map normalized clicks) and schedules
    page.set_viewport_size (async, fire-and-forget). Clamped to sane bounds.
    """
    try:
        w = int(ev.get("width", 0))
        h = int(ev.get("height", 0))
    except (TypeError, ValueError):
        return
    w = max(320, min(w, _TEACH_MAX_W))
    h = max(240, min(h, _TEACH_MAX_H))
    if w == viewport["w"] and h == viewport["h"]:
        return
    viewport["w"], viewport["h"] = w, h
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.ensure_future(page.set_viewport_size({"width": w, "height": h}), loop=loop)


def _dispatch_event(ev: dict, adapter: CdpInputAdapter, page) -> None:
    """Translate a client input event into adapter calls or navigation."""
    kind = ev.get("type")
    if kind == "mouse":
        _dispatch_mouse(ev, adapter)
    elif kind == "key":
        _dispatch_key(ev, adapter)
    elif kind == "navigate":
        _dispatch_navigate(ev, page)
    else:
        logger.debug("hermes.training_live.recv.unknown_type type=%r", kind)


def _record_step(orchestrator, session_id, ev: dict) -> None:
    """Capture a demonstrated action as a training step so /sign yields a real
    skill (compile_and_persist reads the orchestrator's steps). Best-effort: if
    the session is not RECORDING / not found, skip silently. Only meaningful
    actions are recorded (navigate, click, key) — pointer moves/releases are
    noise and are dropped."""
    if orchestrator is None or session_id is None:
        return
    kind = ev.get("type")
    payload: dict | None = None
    if kind == "navigate":
        url = str(ev.get("url", "")).strip()
        if url:
            payload = {"kind": "navigate", "url": url}
    elif kind == "mouse" and ev.get("action") == "down":
        payload = {
            "kind": "act", "action": "click",
            "x": ev.get("x"), "y": ev.get("y"), "button": ev.get("button", 0),
        }
        el = ev.get("element")
        if isinstance(el, dict) and el:
            payload["element"] = el  # semantic descriptor (tag/role/text) if resolved
    elif kind == "key" and ev.get("action") in ("down", "char"):
        text = ev.get("text")
        if isinstance(text, str) and text:
            payload = {"kind": "act", "action": "key", "text": text}
    if payload is None:
        return
    try:
        orchestrator.capture_step(
            session_id=session_id,
            surface_kind=SurfaceKind.BROWSER,
            action_payload=payload,
        )
        logger.info(
            "hermes.training_live.record_step.ok kind=%s session=%s", kind, session_id
        )
    except Exception:  # noqa: BLE001 — surface the reason (silent skip hid a real bug)
        logger.warning(
            "hermes.training_live.record_step.FAILED kind=%s session=%s payload=%r",
            kind, session_id, payload, exc_info=True,
        )


def _parse_uuid(raw: str) -> "UUID | None":
    try:
        return UUID(raw)
    except (ValueError, TypeError, AttributeError):
        return None


def _dispatch_mouse(ev: dict, adapter: CdpInputAdapter) -> None:
    action = ev.get("action", "")
    try:
        x = float(ev["x"])
        y = float(ev["y"])
    except (KeyError, TypeError, ValueError):
        return

    if action == "move":
        adapter.pointer_motion(x, y)
    elif action in ("down", "up"):
        btn_idx = int(ev.get("button", 0))
        evdev_code = _BTN_MAP.get(btn_idx, BTN_LEFT)
        # Move to position first so the click lands on the right element.
        adapter.pointer_motion(x, y)
        adapter.pointer_button(evdev_code, action == "down")
    else:
        logger.debug("hermes.training_live.recv.mouse_unknown_action action=%r", action)


def _dispatch_key(ev: dict, adapter: CdpInputAdapter) -> None:
    action = ev.get("action", "")
    text = ev.get("text")
    # Browsers cannot provide X11 keysyms, so the web UI sends ev.key as `text`.
    # A single printable char → insert as text (char event, on press only). A
    # NAMED key (Enter, Backspace, Tab, Escape, Arrow*, Delete, …) → CDP keyDown/
    # keyUp with the same name (CDP `key` == DOM KeyboardEvent.key).
    if isinstance(text, str) and text:
        if len(text) == 1:
            if action in ("down", "char"):
                adapter.keyboard_keysym(ord(text), pressed=True)
            return
        adapter.keyboard_key(text, pressed=(action == "down"))
        return

    # Back-compat: explicit X11 keysym path (e.g. mutter/desktop clients).
    raw_keysym = ev.get("keysym")
    if raw_keysym is None:
        return
    try:
        keysym = int(raw_keysym)
    except (TypeError, ValueError):
        return
    adapter.keyboard_keysym(keysym, pressed=(action == "down"))


def _dispatch_navigate(ev: dict, page) -> None:
    url = str(ev.get("url", "")).strip()
    if not url:
        return
    # Schedule navigation as a fire-and-forget; we do not await here to keep
    # the recv loop responsive.  Navigation failures are logged by Playwright.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.ensure_future(page.goto(url), loop=loop)


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------


async def _close_context_safe(ctx) -> None:
    try:
        await ctx.close()
    except Exception:  # noqa: BLE001
        logger.debug("hermes.training_live.ctx_close_error", exc_info=True)


async def _stop_playwright_safe(pw) -> None:
    try:
        await pw.stop()
    except Exception:  # noqa: BLE001
        logger.debug("hermes.training_live.pw_stop_error", exc_info=True)
