"""CdpInputAdapter — SeatInputEffectorPort over CDP Input.dispatch* methods.

Injects pointer and keyboard events into the jailed Chromium page via CDP,
matching the SeatInputEffectorPort protocol consumed by MirrorServer and
SessionInputBridge.

Seam B (spec 012): when the operator takes control from the mirror WebSocket,
input events flow here instead of through mutter RemoteDesktop D-Bus, because
the sandbox Chromium is not a Wayland client of the host compositor.

Coordinate system:
    MirrorServer sends pointer coordinates in the compositor frame (relative to
    the mutter display). For the CDP adapter the viewport coordinates ARE the
    same as the compositor coordinates (the screencast uses the page's layout
    viewport, not the OS screen). Callers that produce coordinates from the
    screencast frame can pass them directly.

Key injection:
    pointer_motion / pointer_button / pointer_axis_discrete map to
    Input.dispatchMouseEvent (CDP spec).

    keyboard_keysym maps to Input.dispatchKeyEvent using type='char' for
    printable Unicode codepoints. Non-printable keysyms are silently ignored
    (CDP char-based injection cannot represent raw hardware scancodes without
    the Key* mapping tables; for full keycode support the Wayland compositor
    path — MutterMirrorSession — remains the reference).

    keyboard_keycode is NOT supported via CDP (evdev codes have no CDP
    equivalent without a layout table). The method is a no-op and logs a
    warning; callers should prefer keysym injection for CDP targets.

Thread-safety:
    All CDP send() calls are coroutines and must run in the event loop.
    The SeatInputEffectorPort contract allows synchronous calls; therefore
    this adapter exposes a synchronous facade that schedules the coroutine
    via asyncio.ensure_future and returns immediately (fire-and-forget).
    For the mirror server this is safe: input is not acked to the sender.

    If the event loop is NOT running (unit test context), calls are silently
    no-ops — this is intentional; test coverage uses the async helpers directly.

Ownership:
    The adapter does NOT own the CDPSession; it receives one already attached.
    call stop() to signal that the adapter is no longer in use (no-op here
    since the caller manages the session lifecycle).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import CDPSession

logger = logging.getLogger(__name__)

_BUTTON_NAME: dict[int, str] = {
    0: "left",
    1: "middle",
    2: "right",
}

# X11 keysym → printable text (ASCII range only; Unicode handled generically).
_PRINTABLE_LOW: int = 0x0020
_PRINTABLE_HIGH: int = 0x007E
_UNICODE_KEYSYM_BASE: int = 0x01000000

# DOM KeyboardEvent.key → (windowsVirtualKeyCode, DOM code). Chrome only executes
# the built-in editing command for these keys when the legacy VK code is present.
_NAMED_KEY_META: dict[str, tuple[int, str]] = {
    "Enter": (13, "Enter"),
    "Backspace": (8, "Backspace"),
    "Tab": (9, "Tab"),
    "Escape": (27, "Escape"),
    "Delete": (46, "Delete"),
    "ArrowLeft": (37, "ArrowLeft"),
    "ArrowUp": (38, "ArrowUp"),
    "ArrowRight": (39, "ArrowRight"),
    "ArrowDown": (40, "ArrowDown"),
    "Home": (36, "Home"),
    "End": (35, "End"),
    "PageUp": (33, "PageUp"),
    "PageDown": (34, "PageDown"),
}


class CdpInputAdapter:
    """Inject input events into a jailed Chromium page via CDP.

    Implements SeatInputEffectorPort (structurally; no explicit import of the
    Protocol to avoid import cycles — the Protocol lives in shell_server/mirror).

    Args:
        session:  An attached CDPSession for the target page.
    """

    def __init__(self, *, session: "CDPSession") -> None:
        self._session = session
        # CDP is STATELESS per event: a mousePressed/mouseReleased dispatches at
        # the x,y IN THAT EVENT — it does NOT reuse the position of a prior
        # mouseMoved. We remember the last pointer position (set synchronously by
        # pointer_motion) so the press/release lands where the operator clicked
        # instead of the (0,0) top-left corner. Without this the click never hits
        # the target element → the field never focuses → typing goes nowhere.
        self._last_x: float = 0.0
        self._last_y: float = 0.0

    # ------------------------------------------------------------------
    # SeatInputEffectorPort — synchronous facade
    # ------------------------------------------------------------------

    def pointer_motion(self, x: float, y: float) -> None:
        """Move pointer to absolute (x, y) in page viewport coordinates."""
        self._last_x = float(x)
        self._last_y = float(y)
        self._fire(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y},
        )

    def pointer_button(self, button: int, pressed: bool) -> None:
        """Press or release a pointer button (evdev BTN_LEFT=272, BTN_RIGHT=273...).

        The BTN_* evdev codes (272=left, 273=right, 274=middle) are mapped to
        CDP button names. Unmapped codes default to 'left'.

        The press/release is dispatched at the LAST pointer position recorded by
        pointer_motion — CDP does not carry position across events, so sending
        (0,0) here would click the top-left corner (the historical "clicks do
        nothing / can't type" bug). clickCount stays 1 on release too so Chrome
        synthesises the DOM 'click' (press and release must agree).
        """
        # evdev → CDP button name: BTN_LEFT=272 → 0, RIGHT=273 → 2, MIDDLE=274 → 1
        evdev_to_idx = {272: 0, 273: 2, 274: 1}
        btn_idx = evdev_to_idx.get(button, 0)
        btn_name = _BUTTON_NAME.get(btn_idx, "left")
        event_type = "mousePressed" if pressed else "mouseReleased"
        self._fire(
            "Input.dispatchMouseEvent",
            {
                "type": event_type,
                "x": self._last_x,
                "y": self._last_y,
                "button": btn_name,
                "clickCount": 1,
            },
        )

    def pointer_axis_discrete(self, axis: int, steps: int) -> None:
        """Scroll by discrete steps. axis: 0=vertical, 1=horizontal."""
        delta_x = float(steps * 120) if axis == 1 else 0.0
        delta_y = float(steps * 120) if axis == 0 else 0.0
        self._fire(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": 0.0,
                "y": 0.0,
                "deltaX": delta_x,
                "deltaY": delta_y,
            },
        )

    def keyboard_keysym(self, keysym: int, pressed: bool) -> None:
        """Inject a key event by X11 keysym via CDP 'char' event (press only).

        Only press events (pressed=True) emit a 'char' event; release events
        are no-ops because CDP char injection has no 'keyUp' counterpart that
        carries text — the browser does not need it for text insertion.

        Non-printable keysyms (outside ASCII 0x20-0x7E and X11 Unicode range
        0x01000000+) are silently dropped.
        """
        if not pressed:
            return
        char = _keysym_to_char(keysym)
        if char is None:
            logger.debug(
                "hermes.cdp_input_adapter.keysym_not_printable keysym=0x%x", keysym
            )
            return
        self._fire("Input.dispatchKeyEvent", {"type": "char", "text": char})

    def keyboard_key(self, key: str, pressed: bool) -> None:
        """Inject a NAMED key (Enter, Backspace, Tab, Escape, Arrow*, Delete, …)
        via CDP keyDown/keyUp. CDP's ``key`` field uses the same names as the DOM
        ``KeyboardEvent.key``, so a browser's ``ev.key`` for non-printable keys maps
        1:1. Printable single chars should use ``keyboard_keysym`` (char insertion).

        For editing keys Chrome only runs the built-in command (delete a char,
        submit a form, move the caret) when the event also carries the legacy
        ``windowsVirtualKeyCode``/``code``. A bare ``key`` fires a JS keydown but
        does NOT edit the field — so Backspace/Enter/arrows appeared dead. We
        attach the VK/code for the keys we know; unknown named keys still send
        the bare ``key`` (best-effort).
        """
        if not key:
            return
        params: dict[str, Any] = {
            "type": "keyDown" if pressed else "keyUp",
            "key": key,
        }
        meta = _NAMED_KEY_META.get(key)
        if meta is not None:
            vk, code = meta
            params["windowsVirtualKeyCode"] = vk
            params["nativeVirtualKeyCode"] = vk
            params["code"] = code
        self._fire("Input.dispatchKeyEvent", params)

    def keyboard_keycode(self, keycode: int, pressed: bool) -> None:  # noqa: ARG002
        """No-op: evdev keycodes have no direct CDP mapping without a layout table.

        Callers that need full keycode injection should use MutterMirrorSession
        (Wayland compositor path) instead of this adapter.
        """
        logger.warning(
            "hermes.cdp_input_adapter.keycode_unsupported "
            "keycode=%d — use keyboard_keysym or MutterMirrorSession for raw keycodes",
            keycode,
        )

    def stop(self) -> None:
        """No-op: caller owns the CDPSession lifecycle."""

    # ------------------------------------------------------------------
    # Async helpers (for tests and direct use)
    # ------------------------------------------------------------------

    async def async_mouse_click(
        self, x: float, y: float, *, button: str = "left"
    ) -> None:
        """Click at (x, y) with full press/release sequence."""
        common = {"x": x, "y": y, "button": button, "clickCount": 1}
        await self._session.send(
            "Input.dispatchMouseEvent", {"type": "mousePressed", **common}
        )
        await self._session.send(
            "Input.dispatchMouseEvent", {"type": "mouseReleased", **common}
        )

    async def resolve_element_at(self, x: float, y: float) -> dict | None:
        """Return a semantic descriptor of the element at viewport (x, y), or None.

        Teaching capture records clicks as raw coordinates ('click at (640,300)') —
        brittle and not reusable. Resolving the actual element (via the page's own
        elementFromPoint over the SAME CDP session) lets the skill say 'click the
        Search button', which survives layout changes. Best-effort: any CDP/JS error
        returns None and the caller falls back to coordinates.
        """
        js = (
            "(function(){var el=document.elementFromPoint(" + str(int(x)) + "," + str(int(y)) + ");"
            "if(!el)return null;"
            "var t=(el.innerText||el.value||el.getAttribute('aria-label')||"
            "el.getAttribute('placeholder')||el.getAttribute('title')||el.name||'').trim().slice(0,80);"
            "return JSON.stringify({tag:el.tagName.toLowerCase(),"
            "role:el.getAttribute('role'),text:t,"
            "id:el.id||null,name:el.getAttribute('name')||null});})()"
        )
        try:
            res = await self._session.send(
                "Runtime.evaluate", {"expression": js, "returnByValue": True}
            )
            val = (res.get("result") or {}).get("value")
            return json.loads(val) if val else None
        except Exception:  # noqa: BLE001 — never block capture on resolution
            logger.debug("hermes.cdp_input_adapter.resolve_element_at failed", exc_info=True)
            return None

    async def async_type_text(self, text: str) -> None:
        """Type a string using CDP char events (one per character)."""
        for ch in text:
            await self._session.send(
                "Input.dispatchKeyEvent", {"type": "char", "text": ch}
            )

    async def async_scroll(
        self, x: float, y: float, *, delta_x: float = 0.0, delta_y: float = 0.0
    ) -> None:
        """Dispatch a mouseWheel event at (x, y)."""
        await self._session.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": x,
                "y": y,
                "deltaX": delta_x,
                "deltaY": delta_y,
            },
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fire(self, method: str, params: dict[str, Any]) -> None:
        """Schedule a CDP send as a fire-and-forget future in the running loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — unit test or synchronous context; skip silently.
            logger.debug(
                "hermes.cdp_input_adapter.no_loop method=%s — skipped", method
            )
            return
        asyncio.ensure_future(self._session.send(method, params), loop=loop)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _keysym_to_char(keysym: int) -> str | None:
    """Convert an X11 keysym to a printable string, or None if unprintable.

    ASCII printable range: keysym == ord(char) for 0x0020..0x007E.
    Unicode extension range: keysym = 0x01000000 | codepoint.
    """
    if _PRINTABLE_LOW <= keysym <= _PRINTABLE_HIGH:
        return chr(keysym)
    if keysym >= _UNICODE_KEYSYM_BASE:
        cp = keysym & 0x00FFFFFF
        try:
            return chr(cp)
        except (ValueError, OverflowError):
            return None
    return None
