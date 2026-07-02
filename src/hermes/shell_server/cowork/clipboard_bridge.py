"""clipboard_bridge — UTF-8 copy/paste between the user's machine and the JAILED
browser shown via noVNC, WITHOUT relying on x11vnc's clipboard.

Why not x11vnc: x11vnc 0.9.16, as an X selection owner, does not answer the TARGETS
request Chromium sends before pasting → Chromium's paste HANGS. Its X→client path also
never emits ServerCutText on the headless Xvfb. Measured, both directions dead.

Instead we bridge through CDP against the shared jailed Chromium (Chromium's own
clipboard/DOM works perfectly on X):
  - PASTE  (outside → jail): Input.insertText inserts the text at the focused element
    (UTF-8 native, no clipboard, no TARGETS negotiation).
  - COPY   (jail → outside): read window.getSelection() of the focused page.

The frontend (VncView) intercepts Ctrl/Cmd+V and Ctrl/Cmd+C before noVNC forwards them
to RFB, and calls these endpoints; so the real keystrokes never reach x11vnc. Auth is
the shell web-ui bearer token, same as teach_vnc.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger("hermes.shell_server.cowork.clipboard_bridge")

# Cap the payload so a huge accidental paste can't hammer the renderer.
_MAX_PASTE_CHARS = 100_000


def create_clipboard_bridge_router() -> APIRouter:
    from hermes.shell_server.cowork.training_live import (  # noqa: PLC0415
        _cdp_url,
        _try_ensure_browser_running,
        _verify_token,
    )

    router = APIRouter()

    def _auth(request: Request) -> None:
        expected = getattr(request.app.state, "shell_webui_token", "")
        auth = request.headers.get("authorization", "")
        tok = auth[7:] if auth[:7].lower() == "bearer " else ""
        if not _verify_token(tok, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    async def _pages(browser):
        return [p for ctx in browser.contexts for p in ctx.pages]

    async def _focused_page(browser):
        """The page that currently has OS focus (the tab the user sees in noVNC);
        fall back to the first page."""
        pages = await _pages(browser)
        for p in pages:
            try:
                if await p.evaluate("document.hasFocus()"):
                    return p
            except Exception:  # noqa: BLE001
                continue
        return pages[0] if pages else None

    async def _connect():
        await _try_ensure_browser_running()
        from playwright.async_api import async_playwright  # noqa: PLC0415

        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(_cdp_url())
        except Exception:  # noqa: BLE001
            await pw.stop()
            raise HTTPException(status_code=503, detail="browser unavailable")
        return pw, browser

    @router.post("/api/v1/clipboard/paste")
    async def clipboard_paste(request: Request) -> dict:
        _auth(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        text = str((body or {}).get("text", ""))[:_MAX_PASTE_CHARS]
        if not text:
            return {"ok": True, "inserted": 0}
        pw, browser = await _connect()
        try:
            page = await _focused_page(browser)
            if page is None:
                raise HTTPException(status_code=503, detail="no page")
            session = await page.context.new_cdp_session(page)
            # Input.insertText = "paste as text" into the focused editable element.
            await session.send("Input.insertText", {"text": text})
            return {"ok": True, "inserted": len(text)}
        finally:
            try:
                await pw.stop()
            except Exception:  # noqa: BLE001
                pass

    # Read the selection of the FOCUSED page only — never scan background tabs (that
    # would leak another tab's selection). Order of checks: the active element's own
    # selection (input/textarea), then the DOM range (window.getSelection), then any
    # input/textarea whose selectionStart/End still holds a range (these survive the
    # blur our fresh CDP connection may cause on the focused field).
    _SELECTION_JS = (
        "(() => {"
        " const a = document.activeElement;"
        " if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA')"
        "   && a.selectionStart != null && a.selectionEnd > a.selectionStart)"
        "   return a.value.substring(a.selectionStart, a.selectionEnd);"
        " const ds = (window.getSelection && window.getSelection().toString()) || '';"
        " if (ds) return ds;"
        " for (const el of document.querySelectorAll('input,textarea')) {"
        "   if (el.selectionStart != null && el.selectionEnd > el.selectionStart)"
        "     return el.value.substring(el.selectionStart, el.selectionEnd);"
        " }"
        " return ''; })()"
    )

    @router.post("/api/v1/clipboard/copy")
    async def clipboard_copy(request: Request) -> dict:
        _auth(request)
        pw, browser = await _connect()
        try:
            page = await _focused_page(browser)
            if page is None:
                return {"ok": True, "text": ""}
            try:
                t = await page.evaluate(_SELECTION_JS)
            except Exception:  # noqa: BLE001
                t = ""
            return {"ok": True, "text": str(t or "")}
        finally:
            try:
                await pw.stop()
            except Exception:  # noqa: BLE001
                pass

    return router
