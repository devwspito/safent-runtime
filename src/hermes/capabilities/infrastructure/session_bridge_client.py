"""SessionBridgeClient — daemon-side async AF_UNIX client to SessionInputBridge.

Runs inside hermes-runtime.service (the hardened daemon) and communicates
with hermes-session-input.service (the session-side helper) over the AF_UNIX
socket at /run/hermes/session-input.sock.

The socket directory is 0700 hermes:hermes (tmpfiles.d/hermes.conf) and is
listed in ReadWritePaths= of hermes-runtime.service, so the daemon can
connect to it despite ProtectSystem=strict.

Authentication: a per-boot token is written to /run/hermes/session-input.token
by the session bridge at startup (mode 0600, hermes-user owned but the
/run/hermes directory is 0700 hermes:hermes so the daemon can traverse it to
read the token file — group hermes has rx on the parent dir).

Wire protocol: 4-byte big-endian length prefix + UTF-8 JSON body.
Each call opens a fresh connection, sends one request, reads one response,
and closes. This keeps the client stateless and crash-safe.

Capa: infrastructure (adapts the socket protocol to a typed Python API).
"""

from __future__ import annotations

import asyncio
import json
import struct
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes.capabilities.session_bridge_client")

_SOCKET_PATH = Path("/run/hermes/session-input.sock")
_TOKEN_PATH = Path("/run/hermes/session-input.token")
_CONNECT_TIMEOUT_S: float = 5.0
_IO_TIMEOUT_S: float = 30.0
_MAX_FRAME_BYTES: int = 64 * 1024  # generous limit for screenshot path response


class SessionBridgeUnavailable(RuntimeError):
    """The session bridge is not reachable (unit not started or socket missing)."""


class SessionBridgeError(RuntimeError):
    """The bridge returned ok=False."""


class SessionBridgeClient:
    """Thin async client for SessionInputBridge.

    Each public method opens one connection, sends one request, and returns
    the response dict.  All methods raise SessionBridgeUnavailable when the
    socket is absent and SessionBridgeError when ok=False.
    """

    def __init__(
        self,
        *,
        socket_path: Path = _SOCKET_PATH,
        token_path: Path = _TOKEN_PATH,
    ) -> None:
        self._socket_path = socket_path
        self._token_path = token_path
        self._cached_token: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def screenshot(self) -> dict[str, Any]:
        return await self._call({"verb": "screenshot"})

    async def pointer_motion(self, x: float, y: float) -> dict[str, Any]:
        return await self._call({"verb": "pointer_motion", "x": x, "y": y})

    async def pointer_button(self, btn: int, press: bool) -> dict[str, Any]:
        return await self._call({"verb": "pointer_button", "btn": btn, "press": press})

    async def pointer_axis(self, axis: int, steps: int) -> dict[str, Any]:
        return await self._call({"verb": "pointer_axis", "axis": axis, "steps": steps})

    async def keycode(self, code: int, press: bool) -> dict[str, Any]:
        return await self._call({"verb": "keycode", "code": code, "press": press})

    async def keysym(self, sym: int, press: bool) -> dict[str, Any]:
        """Inject a single key event by X11 keysym value (press or release).

        Added for the cua_driver which synthesises named key presses (Return,
        Escape, modifier hold/release) without going through type_text's
        char-by-char loop.
        """
        return await self._call({"verb": "keysym", "sym": sym, "press": press})

    async def type_text(self, text: str) -> dict[str, Any]:
        return await self._call({"verb": "type_text", "text": text})

    # ------------------------------------------------------------------
    # AT-SPI v2 — element-level GUI control
    # ------------------------------------------------------------------

    async def list_windows(self) -> dict[str, Any]:
        """Return the list of top-level windows from the a11y tree.

        Response: {"ok": true, "windows": [{app_name, pid, window_id, title,
        is_active, bounds}]}.
        Raises SessionBridgeError if the bridge returns ok=False (e.g. pyatspi
        not installed); caller should treat that as a signal to fall back to
        the v1 stub.
        """
        return await self._call({"verb": "list_windows"})

    async def get_window_state(self, window_id: int) -> dict[str, Any]:
        """Return the indexed element tree for a window.

        Response: {"ok": true, "title": str, "elements": [{index, role, name,
        bounds: {x,y,w,h}}]}.
        """
        return await self._call({"verb": "get_window_state", "window_id": window_id})

    async def atspi_click(
        self,
        window_id: int,
        element_index: int,
        double: bool = False,
        button: str = "left",
    ) -> dict[str, Any]:
        """Activate a GUI element by its accessibility index.

        Response on success: {"ok": true}.
        Response when coord-fallback needed: {"ok": true, "bounds": {x,y,w,h}}.
        The caller should check for "bounds" in the response and perform a
        pointer click at the centre of the returned rect.
        """
        return await self._call({
            "verb": "atspi_click",
            "window_id": window_id,
            "element_index": element_index,
            "double": double,
            "button": button,
        })

    async def atspi_type(
        self,
        window_id: int,
        element_index: int,
        text: str,
    ) -> dict[str, Any]:
        """Set the text of an editable element via AT-SPI EditableText.

        Response: {"ok": true}.
        Raises SessionBridgeError if the element has no editable text interface;
        caller should fall back to focus+type_text.
        """
        return await self._call({
            "verb": "atspi_type",
            "window_id": window_id,
            "element_index": element_index,
            "text": text,
        })

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_token(self) -> str:
        """Load token from disk (cached per process lifetime for efficiency)."""
        if self._cached_token is not None:
            return self._cached_token
        try:
            token = self._token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SessionBridgeUnavailable(
                f"Session bridge token not found at {self._token_path}: {exc}"
            ) from exc
        self._cached_token = token
        return token

    async def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Open connection, send request with token, return parsed response."""
        token = self._load_token()
        request = {"token": token, **payload}
        response = await self._roundtrip(request)
        if not response.get("ok", False):
            error = response.get("error", "unknown error from bridge")
            raise SessionBridgeError(f"SessionInputBridge returned error: {error}")
        return response

    async def _roundtrip(self, request: dict[str, Any]) -> dict[str, Any]:
        """Connect, send one frame, receive one frame, close."""
        if not self._socket_path.exists():
            raise SessionBridgeUnavailable(
                f"Session bridge socket not found: {self._socket_path}"
            )
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._socket_path)),
                timeout=_CONNECT_TIMEOUT_S,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise SessionBridgeUnavailable(
                f"Cannot connect to session bridge at {self._socket_path}: {exc}"
            ) from exc

        try:
            await asyncio.wait_for(
                _send_frame(writer, request), timeout=_IO_TIMEOUT_S
            )
            response = await asyncio.wait_for(
                _read_frame(reader), timeout=_IO_TIMEOUT_S
            )
        except asyncio.TimeoutError as exc:
            raise SessionBridgeUnavailable(
                "Session bridge I/O timeout"
            ) from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        if response is None:
            raise SessionBridgeUnavailable("Session bridge closed connection without response")
        return response


# ------------------------------------------------------------------
# Framing helpers (module-level, pure)
# ------------------------------------------------------------------


async def _send_frame(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    writer.write(struct.pack(">I", len(body)) + body)
    await writer.drain()


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        return None
    length = struct.unpack(">I", header)[0]
    if length > _MAX_FRAME_BYTES:
        raise SessionBridgeUnavailable(f"Response frame too large: {length}")
    body = await reader.readexactly(length)
    return json.loads(body.decode("utf-8"))
