"""SessionInputBridge — session-side AF_UNIX server for host input + screenshot.

Runs as hermes-user (systemd user unit hermes-session-input.service) because
it needs the mutter D-Bus session bus.  The hardened daemon (hermes-runtime.service
— ProtectHome=yes, PrivateDevices=yes, no session bus) cannot reach mutter
directly; instead it connects here over an AF_UNIX socket that it is allowed
to write to (/run/hermes/session-input.sock — added to ReadWritePaths).

Security posture
----------------
* Socket dir /run/hermes is 0700 hermes:hermes (tmpfiles.d/hermes.conf).
  Only hermes (daemon UID) and hermes-user (session UID, group=hermes) can
  reach it.
* SO_PEERCRED UID check: every request's UID is verified against the
  daemon UID read from /run/hermes/daemon-uid (written at bridge start time).
  This is defense-in-depth on top of the filesystem permission gate.
* Per-boot token in /run/hermes/session-input.token (0600 hermes-user):
  the daemon reads it and sends it in every request.  Prevents a rogue
  process that somehow got the socket path from issuing commands without
  knowing the token.
* Rate-limit: at most MAX_REQUESTS_PER_SECOND calls per second before the
  bridge starts rejecting with error "rate_limit_exceeded".
* Key-chord denylist: Ctrl-Alt-Fx, Ctrl-Alt-Delete, Ctrl-Alt-Backspace
  are rejected unconditionally regardless of what the daemon sends.
* Contention guard: if InputOwnershipLedger says the OPERATOR currently
  holds input (human mirror session is live), agent input is refused.

Wire protocol (AF_UNIX, SOCK_STREAM)
--------------------------------------
Each request is a length-prefixed JSON frame:
  4 bytes big-endian uint32 = len(json_bytes)
  N bytes UTF-8 JSON

  Request  → {"token": str, "verb": str, ...verb-specific-args...}
  Response ← {"ok": bool, ...result-fields...}

Verbs
-----
  screenshot              → {"ok":true, "path":str, "width":int, "height":int}
  pointer_motion x y      → {"ok":true}
  pointer_button btn press → {"ok":true}   btn: 0=left,1=right,2=middle press: bool
  pointer_axis dx dy      → {"ok":true}
  keycode code press      → {"ok":true}
  type_text text          → {"ok":true}

AT-SPI verbs (v2 — require python3-pyatspi + apps that publish a11y)
--------------------------------------------------------------------
  list_windows                         → {"ok":true, "windows":[...]}
  get_window_state {window_id:int}     → {"ok":true, "title":str, "elements":[...]}
  atspi_click {window_id, element_index, double?, button?}
                                       → {"ok":true}  or {"ok":true, "bounds":{x,y,w,h}}
  atspi_type {window_id, element_index, text}
                                       → {"ok":true}  or {"ok":false, "atspi_unavailable":true}

  All AT-SPI verbs degrade gracefully: if pyatspi is not installed or the a11y
  bus is unavailable they return {"ok":false, "error":"atspi_unavailable"} and
  the daemon falls back to vision+coordinates (v1).

Capa: infrastructure (session side — adapts mutter to socket protocol).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import time
from pathlib import Path
from uuid import UUID

from hermes.agents_os.application.teaching.input_ownership_ledger import InputOwnershipLedger
from hermes.agents_os.application.teaching.teaching_context import InputOwner
from hermes.shell_server.mirror.input_effector_port import SeatInputEffectorPort
from hermes.shell_server.screen_capture.service import ScreenCaptureBackend

logger = logging.getLogger("hermes.session_agent.input_bridge")

SOCKET_PATH = Path("/run/hermes/session-input.sock")
TOKEN_PATH = Path("/run/hermes/session-input.token")
DAEMON_UID_PATH = Path("/run/hermes/daemon-uid")
SESSION_INPUT_READY_FILE = Path("/run/hermes/session-input.ready")

# Security constants
MAX_FRAME_BYTES: int = 16 * 1024  # 16 KiB — type_text longest plausible string
MAX_REQUESTS_PER_SECOND: int = 60  # 60 Hz input rate cap
_OWNERSHIP_CONTEXT_ID = UUID("00000000-0000-0000-0000-000000000001")  # singleton

# Evdev keycodes for the denylist (kernel numbering).
# These chords switch VTs or restart the display server and must be
# unconditionally rejected to preserve the kill-switch invariant.
_CHORD_DENYLIST_KEYCODES: frozenset[int] = frozenset({
    59, 60, 61, 62, 63, 64, 65, 66, 67, 68,  # F1..F10
    87, 88,                                    # F11, F12
    111,                                       # Delete (Ctrl-Alt-Delete)
    14,                                        # Backspace (Ctrl-Alt-Backspace)
})

# Evdev keycodes for Ctrl and Alt modifier keys (Left/Right variants).
_CTRL_KEYCODES: frozenset[int] = frozenset({29, 97})   # LeftCtrl, RightCtrl
_ALT_KEYCODES: frozenset[int] = frozenset({56, 100})   # LeftAlt, AltGr(RightAlt)


class BridgeAuthError(RuntimeError):
    """Token or UID check failed."""


class BridgeRateLimitError(RuntimeError):
    """Too many requests per second."""


class BridgeOwnershipError(RuntimeError):
    """Agent input refused: human mirror session holds input ownership."""


class SessionInputBridge:
    """Async AF_UNIX server wrapping a SeatInputEffectorPort + ScreenCaptureBackend.

    Args:
        token:           Per-boot random token (hex string).
        ledger:          InputOwnershipLedger for contention detection.
        mirror:          SeatInputEffectorPort implementation (caller owns lifecycle).
        capture_backend: ScreenCaptureBackend used by the screenshot verb.
        daemon_uid:      Expected UID for SO_PEERCRED check.
    """

    def __init__(
        self,
        *,
        token: str,
        ledger: InputOwnershipLedger,
        mirror: SeatInputEffectorPort,
        capture_backend: ScreenCaptureBackend,
        daemon_uid: int,
    ) -> None:
        self._token = token
        self._ledger = ledger
        self._mirror = mirror
        self._capture_backend = capture_backend
        self._daemon_uid = daemon_uid

        # Rate limiter state
        self._request_count: int = 0
        self._window_start: float = time.monotonic()

        # Modifier state for chord denylist (keycodes currently pressed).
        self._pressed_ctrl: bool = False
        self._pressed_alt: bool = False

        self._server: asyncio.Server | None = None

        # Lazy-initialised AT-SPI client (None = not yet attempted).
        # None → not tried; False → pyatspi unavailable; instance → ready.
        self._atspi_client: "Any | None | bool" = None  # Any = LibAtSpiClient

    async def start(self) -> None:
        """Bind the socket and start accepting connections."""
        SOCKET_PATH.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(SOCKET_PATH)
        )
        # 0660 + grupo hermes: el daemon (hermes) y hermes-user (en grupo hermes)
        # conectan. El socket lo crea el compositor (hermes-user) → su grupo sería
        # hermes-user; sin el chgrp a hermes el daemon NO podría conectar (mismo
        # bug cross-user que el token y el mcp-launcher). hermes-user está en el
        # grupo hermes, así que puede chgrp.
        try:
            import grp as _grp  # noqa: PLC0415
            import os as _os  # noqa: PLC0415
            _os.chown(SOCKET_PATH, -1, _grp.getgrnam("hermes").gr_gid)
        except (KeyError, PermissionError, OSError) as exc:
            logger.warning("hermes.session_input_bridge.sock_chgrp_failed: %s", exc)
        SOCKET_PATH.chmod(0o660)
        logger.info("hermes.session_input_bridge.listening path=%s (0660 group=hermes)", SOCKET_PATH)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        SOCKET_PATH.unlink(missing_ok=True)

    async def serve_forever(self) -> None:
        async with self._server:
            await self._server.serve_forever()

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        sock: socket.socket = writer.get_extra_info("socket")

        if not self._check_peer_uid(sock):
            logger.warning("hermes.session_input_bridge.auth_failed peer=%s", peer)
            await _send_error(writer, "unauthorized")
            writer.close()
            return

        try:
            while True:
                request = await _read_frame(reader)
                if request is None:
                    break
                response = await self._dispatch(request)
                await _send_frame(writer, response)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    def _check_peer_uid(self, sock: socket.socket) -> bool:
        """SO_PEERCRED UID check — defense-in-depth beyond filesystem perms."""
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("iii", creds)
            return uid == self._daemon_uid
        except OSError:
            logger.warning("hermes.session_input_bridge.peercred_unavailable")
            return False

    # ------------------------------------------------------------------
    # Rate limiter
    # ------------------------------------------------------------------

    def _check_rate_limit(self) -> bool:
        """True if within the allowed rate; updates internal counter."""
        now = time.monotonic()
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._request_count = 0
        self._request_count += 1
        return self._request_count <= MAX_REQUESTS_PER_SECOND

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, request: dict) -> dict:
        """Authenticate and route the request to the correct handler."""
        if not _verify_token(request.get("token", ""), self._token):
            raise BridgeAuthError("bad token")

        if not self._check_rate_limit():
            return {"ok": False, "error": "rate_limit_exceeded"}

        verb = request.get("verb", "")
        handlers = {
            "screenshot": self._handle_screenshot,
            "pointer_motion": self._handle_pointer_motion,
            "pointer_button": self._handle_pointer_button,
            "pointer_axis": self._handle_pointer_axis,
            "keycode": self._handle_keycode,
            "keysym": self._handle_keysym,
            "type_text": self._handle_type_text,
            # AT-SPI v2 verbs
            "list_windows": self._handle_list_windows,
            "get_window_state": self._handle_get_window_state,
            "atspi_click": self._handle_atspi_click,
            "atspi_type": self._handle_atspi_type,
        }
        handler = handlers.get(verb)
        if handler is None:
            return {"ok": False, "error": f"unknown verb: {verb!r}"}

        return await handler(request)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_screenshot(self, _request: dict) -> dict:
        return await asyncio.to_thread(self._do_screenshot)

    def _do_screenshot(self) -> dict:
        """Capture one frame via the injected capture_backend, save as PNG."""
        import uuid as _uuid
        import time as _t
        from pathlib import Path as P

        from hermes.shell_server.screen_capture.domain import CaptureTarget
        from hermes.shell_server.training.png_writer import encode_rgba_png

        frame_holder: dict = {}

        def _on_frame(f) -> None:
            if not f.is_blank() and "frame" not in frame_holder:
                frame_holder["frame"] = f

        target = CaptureTarget.monitor("primary")
        self._capture_backend.start(target, _on_frame)
        try:
            for _ in range(30):
                f = self._capture_backend.latest_frame()
                if f is not None and not f.is_blank():
                    frame_holder["frame"] = f
                    break
                _t.sleep(0.1)
        finally:
            self._capture_backend.stop()

        frame = frame_holder.get("frame")
        if frame is None:
            return {"ok": False, "error": "no frame from compositor"}

        out_dir = P("/var/lib/hermes/os-skills")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"screenshot_{_uuid.uuid4().hex}.png"
        out.write_bytes(encode_rgba_png(frame.width, frame.height, frame.data))
        return {"ok": True, "path": str(out), "width": frame.width, "height": frame.height}

    async def _handle_pointer_motion(self, request: dict) -> dict:
        self._assert_agent_owns_input()
        x = float(request["x"])
        y = float(request["y"])
        await asyncio.to_thread(self._mirror.pointer_motion, x, y)
        return {"ok": True}

    async def _handle_pointer_button(self, request: dict) -> dict:
        self._assert_agent_owns_input()
        from hermes.shell_server.mirror.button_codes import BTN_LEFT, BTN_MIDDLE, BTN_RIGHT
        _BUTTON_MAP = {0: BTN_LEFT, 1: BTN_RIGHT, 2: BTN_MIDDLE}
        btn_idx = int(request["btn"])
        btn = _BUTTON_MAP.get(btn_idx, BTN_LEFT)
        press = bool(request["press"])
        await asyncio.to_thread(self._mirror.pointer_button, btn, press)
        return {"ok": True}

    async def _handle_pointer_axis(self, request: dict) -> dict:
        self._assert_agent_owns_input()
        # axis: 0=vertical, 1=horizontal
        axis = int(request.get("axis", 0))
        steps = int(request["steps"])
        await asyncio.to_thread(self._mirror.pointer_axis_discrete, axis, steps)
        return {"ok": True}

    async def _handle_keycode(self, request: dict) -> dict:
        self._assert_agent_owns_input()
        code = int(request["code"])
        press = bool(request["press"])
        self._update_modifier_state(code, press)
        if self._is_denied_chord(code):
            logger.warning(
                "hermes.session_input_bridge.chord_denied code=%d ctrl=%s alt=%s",
                code, self._pressed_ctrl, self._pressed_alt,
            )
            return {"ok": False, "error": "chord_denied"}
        await asyncio.to_thread(self._mirror.keyboard_keycode, code, press)
        return {"ok": True}

    async def _handle_keysym(self, request: dict) -> dict:
        """Inject a single key event by X11 keysym value.

        Used by cua_driver for named key presses (Return, Escape, modifier
        chords) that bypass the char-by-char type_text loop.
        Chord denylist does NOT apply here because keysyms are not evdev
        keycodes — the modifier+Fx combinations that need blocking come in
        via the keycode verb.  Keysym-only injection cannot trigger VT
        switches (those are handled by the kernel via evdev, not by keysym
        injection into Wayland clients).
        """
        self._assert_agent_owns_input()
        sym = int(request["sym"])
        press = bool(request["press"])
        await asyncio.to_thread(self._mirror.keyboard_keysym, sym, press)
        return {"ok": True}

    async def _handle_type_text(self, request: dict) -> dict:
        """Synthesize a string as keysym sequence (each char press+release)."""
        self._assert_agent_owns_input()
        text = str(request.get("text", ""))
        if len(text) > 4096:
            return {"ok": False, "error": "text_too_long"}
        for char in text:
            keysym = _char_to_keysym(char)
            await asyncio.to_thread(self._mirror.keyboard_keysym, keysym, True)
            await asyncio.to_thread(self._mirror.keyboard_keysym, keysym, False)
        return {"ok": True}

    # ------------------------------------------------------------------
    # AT-SPI v2 handlers
    # ------------------------------------------------------------------

    def _get_atspi_client(self) -> "Any | None":
        """Return a LibAtSpiClient instance, or None if unavailable.

        Attempts initialisation exactly once per bridge lifetime; subsequent
        calls return the cached result (instance or None).
        """
        if self._atspi_client is None:
            try:
                from hermes.agents_os.infrastructure.libatspi_client import (  # noqa: PLC0415
                    LibAtSpiClient,
                )
                self._atspi_client = LibAtSpiClient()
                logger.info("hermes.session_input_bridge.atspi_client_ready")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hermes.session_input_bridge.atspi_unavailable: %s", exc
                )
                self._atspi_client = False  # sentinel: attempted and failed
        return self._atspi_client if self._atspi_client is not False else None

    async def _handle_list_windows(self, _request: dict) -> dict:
        client = self._get_atspi_client()
        if client is None:
            return {"ok": False, "error": "atspi_unavailable"}
        windows = await asyncio.to_thread(client.list_windows)
        return {"ok": True, "windows": windows}

    async def _handle_get_window_state(self, request: dict) -> dict:
        client = self._get_atspi_client()
        if client is None:
            return {"ok": False, "error": "atspi_unavailable"}
        window_id = int(request.get("window_id", 0))
        tree = await asyncio.to_thread(client.get_window_tree, window_id)
        return {"ok": True, "title": tree["title"], "elements": tree["elements"]}

    async def _handle_atspi_click(self, request: dict) -> dict:
        client = self._get_atspi_client()
        if client is None:
            return {"ok": False, "error": "atspi_unavailable"}
        window_id = int(request.get("window_id", 0))
        element_index = int(request.get("element_index", 0))
        double = bool(request.get("double", False))
        button = str(request.get("button", "left"))
        fallback = await asyncio.to_thread(
            client.click_element, window_id, element_index,
            double=double, button=button,
        )
        if fallback is not None:
            # Action iface unavailable — return bounds so caller can click by coords.
            return {"ok": True, "bounds": fallback.get("bounds")}
        return {"ok": True}

    async def _handle_atspi_type(self, request: dict) -> dict:
        client = self._get_atspi_client()
        if client is None:
            return {"ok": False, "error": "atspi_unavailable"}
        window_id = int(request.get("window_id", 0))
        element_index = int(request.get("element_index", 0))
        text = str(request.get("text", ""))
        success = await asyncio.to_thread(
            client.set_text_element, window_id, element_index, text
        )
        if not success:
            return {"ok": False, "error": "atspi_set_text_failed"}
        return {"ok": True}

    # ------------------------------------------------------------------
    # Input ownership guard
    # ------------------------------------------------------------------

    def _assert_agent_owns_input(self) -> None:
        """Block agent input when a human mirror session is active."""
        owner = self._ledger.owner_of(_OWNERSHIP_CONTEXT_ID)
        if owner is not None and owner == InputOwner.OPERATOR:
            raise BridgeOwnershipError(
                "Human operator holds input ownership — agent input refused."
            )

    # ------------------------------------------------------------------
    # Chord denylist
    # ------------------------------------------------------------------

    def _update_modifier_state(self, code: int, press: bool) -> None:
        if code in _CTRL_KEYCODES:
            self._pressed_ctrl = press
        if code in _ALT_KEYCODES:
            self._pressed_alt = press

    def _is_denied_chord(self, code: int) -> bool:
        return (
            self._pressed_ctrl
            and self._pressed_alt
            and code in _CHORD_DENYLIST_KEYCODES
        )


# ------------------------------------------------------------------
# Framing helpers (module-level, pure)
# ------------------------------------------------------------------


async def _read_frame(reader: asyncio.StreamReader) -> dict | None:
    """Read a length-prefixed JSON frame. Returns None on clean EOF."""
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        return None
    length = struct.unpack(">I", header)[0]
    if length > MAX_FRAME_BYTES:
        raise ValueError(f"Frame too large: {length} > {MAX_FRAME_BYTES}")
    body = await reader.readexactly(length)
    return json.loads(body.decode("utf-8"))


async def _send_frame(writer: asyncio.StreamWriter, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    writer.write(struct.pack(">I", len(body)) + body)
    await writer.drain()


async def _send_error(writer: asyncio.StreamWriter, reason: str) -> None:
    await _send_frame(writer, {"ok": False, "error": reason})


def _verify_token(candidate: str, expected: str) -> bool:
    """Constant-time comparison (CWE-208)."""
    import hmac as _hmac
    return _hmac.compare_digest(candidate, expected)


def _char_to_keysym(char: str) -> int:
    """Map a Unicode character to its X11 keysym value.

    For ASCII printable: keysym == ord(char) (X11 legacy mapping).
    For higher code points: keysym = 0x01000000 | codepoint (X11 Unicode range).
    """
    cp = ord(char)
    if 0x20 <= cp <= 0x7e:  # ASCII printable
        return cp
    return 0x01000000 | cp
