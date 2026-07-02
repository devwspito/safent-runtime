"""clipboard_bridge — same-origin proxy to the jailed browser's clipboard server.

The jailed browser runs hermes-clipboard-server (xclip) on the veth IP 10.200.0.2:7519,
reachable only by the daemon (nftables). This router exposes it to the web UI behind the
web-ui bearer token, same-origin, so the noVNC clipboard sync has no 2nd URL / CORS:

  GET  /api/v1/clipboard        → the jailed browser's X CLIPBOARD  → {"text": "..."}
  POST /api/v1/clipboard {text} → set the jailed browser's X CLIPBOARD

The web UI (VncView) intercepts Cmd/Ctrl+V, POSTs the local clipboard here, then injects
a REAL Ctrl+V over RFB so the focused app pastes it; and polls GET to mirror in-jail
copies back to the user's local clipboard. This is the OS-edition model (a clipboard
server in the "OS" exposed same-origin), adapted with xclip as the selection owner —
x11vnc's own clipboard can't answer Chromium's TARGETS request (paste hangs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger("hermes.shell_server.cowork.clipboard_bridge")

_CLIP_URL = os.environ.get("BROWSER_CLIP_URL", "http://10.200.0.2:7519").rstrip("/")
_MAX = 100_000
_TIMEOUT = 5


def _get_clipboard() -> str:
    try:
        with urllib.request.urlopen(f"{_CLIP_URL}/clipboard", timeout=_TIMEOUT) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
        return str(body.get("text", ""))
    except Exception:  # noqa: BLE001
        return ""


def _set_clipboard(text: str) -> bool:
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{_CLIP_URL}/clipboard", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            r.read()
        return True
    except Exception:  # noqa: BLE001
        return False


def create_clipboard_bridge_router() -> APIRouter:
    from hermes.shell_server.cowork.training_live import _verify_token  # noqa: PLC0415

    router = APIRouter()

    def _auth(request: Request) -> None:
        expected = getattr(request.app.state, "shell_webui_token", "")
        auth = request.headers.get("authorization", "")
        tok = auth[7:] if auth[:7].lower() == "bearer " else ""
        if not _verify_token(tok, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    @router.get("/api/v1/clipboard")
    async def clipboard_get(request: Request) -> dict:
        _auth(request)
        text = await asyncio.to_thread(_get_clipboard)
        return {"ok": True, "text": text}

    @router.post("/api/v1/clipboard")
    async def clipboard_set(request: Request) -> dict:
        _auth(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        text = str((body or {}).get("text", ""))[:_MAX]
        ok = await asyncio.to_thread(_set_clipboard, text)
        return {"ok": ok}

    return router
