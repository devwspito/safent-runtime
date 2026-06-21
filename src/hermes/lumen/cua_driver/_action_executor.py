"""CuaActionExecutor — translates MCP tool calls to SessionBridgeClient commands.

Stateful: capture() and focus_app() set the active PID/window context.
Actions that require an active window check _active_pid first.

Architecture: pure application logic — the bridge client is injected,
all I/O is async, no framework.

v2 AT-SPI strategy
-------------------
list_windows / get_window_state / click (with element_index) / set_value all
route through the AT-SPI bridge verbs when available.  Each method tries the
AT-SPI path first; on SessionBridgeError (bridge returns ok=False, typically
"atspi_unavailable") it degrades to the v1 stub / coordinate path.

Decision: AT-SPI vs coords
  * click(x, y)              → coords (v1), unchanged.
  * click(window_id, element_index) → atspi_click; if bridge returns bounds
                                       (action iface unavailable) → click by
                                       bounds centre via pointer.
  * set_value(window_id, element_index, value) → atspi_type; on failure →
                                                  isError (v1 behaviour).
  * list_windows()           → bridge list_windows; on failure → v1 stub.
  * get_window_state()       → bridge get_window_state; on failure → v1 stub.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import zlib
import struct
from typing import Any

from hermes.capabilities.infrastructure.session_bridge_client import (
    SessionBridgeClient,
    SessionBridgeError,
    SessionBridgeUnavailable,
)
from hermes.shell_server.mirror.button_codes import BTN_LEFT, BTN_MIDDLE, BTN_RIGHT

from ._keysym_map import named_key_to_keysym, is_modifier

logger = logging.getLogger("hermes.lumen.cua_driver")

# Interpolation steps for drag (enough for smooth motion at 60 Hz).
_DRAG_STEPS: int = 20
# JPEG quality for screenshot (matches hermes-agent default).
_JPEG_QUALITY: int = 85
# Fullscreen sentinel dimensions (filled in from first screenshot).
_DEFAULT_WIDTH: int = 1920
_DEFAULT_HEIGHT: int = 1080

# Evdev button index used in MCP → bridge map.
_BTN_INDEX = {0: 0, 1: 1, 2: 2}  # BTN_LEFT=0, BTN_RIGHT=1, BTN_MIDDLE=2


class NoActiveWindowError(RuntimeError):
    """Action called before capture() established an active window."""


class CuaActionExecutor:
    """Stateful executor that maps MCP CUA tool calls to bridge commands.

    Args:
        bridge: Injected SessionBridgeClient (may be shared across calls).
    """

    def __init__(self, bridge: SessionBridgeClient) -> None:
        self._bridge = bridge
        self._active_pid: int | None = None
        self._active_window_id: int | None = None
        self._screen_width: int = _DEFAULT_WIDTH
        self._screen_height: int = _DEFAULT_HEIGHT

    # ------------------------------------------------------------------
    # Window / app management (v2 via AT-SPI, v1 fallback)
    # ------------------------------------------------------------------

    async def list_windows(self, on_screen_only: bool = True) -> dict[str, Any]:
        """Enumerate top-level windows via AT-SPI (v2).

        Falls back to the v1 synthetic fullscreen entry when AT-SPI is
        unavailable (no pyatspi / no a11y bus).
        """
        try:
            resp = await self._bridge.list_windows()
            raw_windows = resp.get("windows", [])
            return {"windows": [_normalise_window_entry(w) for w in raw_windows]}
        except (SessionBridgeError, SessionBridgeUnavailable) as exc:
            logger.debug(
                "cua_driver.list_windows.atspi_unavailable: %s — falling back to v1", exc
            )
        return _list_windows_v1_stub()

    async def list_apps(self) -> dict[str, Any]:
        """v1: enumerate running processes via /proc.

        Returns basic name+pid pairs; does not filter to GUI apps.
        """
        apps = await asyncio.to_thread(_enumerate_proc_apps)
        return {"apps": apps}

    async def get_window_state(
        self, pid: int | None = None, window_id: int | None = None
    ) -> tuple[str, None]:
        """Return an indexed element tree for the window (v2).

        Formats the AT-SPI tree as a text summary (same contract as the MCP
        tool schema: string + optional image).  Falls back to the v1 stub
        summary when AT-SPI is unavailable.
        """
        if window_id is not None:
            try:
                resp = await self._bridge.get_window_state(int(window_id))
                summary = _format_window_tree(resp)
                return summary, None
            except (SessionBridgeError, SessionBridgeUnavailable) as exc:
                logger.debug(
                    "cua_driver.get_window_state.atspi_unavailable: %s — falling back to v1",
                    exc,
                )
        summary = (
            f'Window pid={pid} window_id={window_id}\n'
            'AXWindow "(LumenSO Desktop)"\n'
            "(v1: full AT-SPI accessibility tree requires v2)"
        )
        return summary, None

    # ------------------------------------------------------------------
    # Capture — establishes active context
    # ------------------------------------------------------------------

    async def capture(
        self, window_id: int | None = None, fmt: str = "jpeg", quality: int = _JPEG_QUALITY
    ) -> dict[str, Any]:
        """Take a screenshot and set the active window context.

        Returns an MCP image content part (type='image', data=base64, mimeType).
        """
        raw = await self._take_screenshot()
        if raw is None:
            return {"error": "No frame available from compositor — bridge unreachable?"}

        self._screen_width = raw["width"]
        self._screen_height = raw["height"]
        self._active_pid = 0
        self._active_window_id = 0

        image_bytes = _encode_image(raw["data"], raw["width"], raw["height"], fmt, quality)
        mime = "image/jpeg" if fmt == "jpeg" else "image/png"
        return {
            "type": "image",
            "data": base64.b64encode(image_bytes).decode("ascii"),
            "mimeType": mime,
        }

    # ------------------------------------------------------------------
    # Pointer actions
    # ------------------------------------------------------------------

    async def click(
        self,
        x: float,
        y: float,
        button: int = 0,
        count: int = 1,
        *,
        window_id: int | None = None,
        element_index: int | None = None,
    ) -> dict[str, Any]:
        """Click at coordinates (v1) or at an accessibility element (v2).

        When window_id + element_index are provided the click is routed
        through AT-SPI (atspi_click).  If the bridge signals that the action
        interface is unavailable it returns coords via "bounds"; we then
        click the centre of those bounds via pointer (seamless degradation).
        When only x/y are provided the pointer path is used directly.
        """
        self._require_active_window()

        if window_id is not None and element_index is not None:
            return await self._click_by_element(
                window_id, element_index, button=button, count=count
            )

        for _ in range(count):
            await self._bridge.pointer_motion(x, y)
            await self._bridge.pointer_button(button, True)
            await self._bridge.pointer_button(button, False)
        return {"ok": True, "message": f"click({x}, {y}) ×{count}"}

    async def drag(
        self,
        from_x: float, from_y: float,
        to_x: float, to_y: float,
    ) -> dict[str, Any]:
        self._require_active_window()
        await self._bridge.pointer_motion(from_x, from_y)
        await self._bridge.pointer_button(0, True)
        for step in range(1, _DRAG_STEPS + 1):
            t = step / _DRAG_STEPS
            ix = from_x + (to_x - from_x) * t
            iy = from_y + (to_y - from_y) * t
            await self._bridge.pointer_motion(ix, iy)
        await self._bridge.pointer_button(0, False)
        return {"ok": True, "message": f"drag({from_x},{from_y})→({to_x},{to_y})"}

    async def scroll(
        self, direction: str, amount: int, x: float | None = None, y: float | None = None
    ) -> dict[str, Any]:
        self._require_active_window()
        if x is not None and y is not None:
            await self._bridge.pointer_motion(x, y)
        axis, steps = _scroll_to_axis_steps(direction, amount)
        await self._bridge.pointer_axis(axis, steps)
        return {"ok": True, "message": f"scroll {direction} {amount}"}

    # ------------------------------------------------------------------
    # Keyboard actions
    # ------------------------------------------------------------------

    async def type_text(self, text: str) -> dict[str, Any]:
        self._require_active_window()
        await self._bridge.type_text(text)
        return {"ok": True, "message": f"type_text len={len(text)}"}

    async def press_key(self, key: str) -> dict[str, Any]:
        self._require_active_window()
        keysym = named_key_to_keysym(key)
        await self._send_keysym(keysym)
        return {"ok": True, "message": f"press_key {key!r}"}

    async def hotkey(self, keys: list[str]) -> dict[str, Any]:
        """Press modifier(s) + primary key as a chord.

        Modifiers are held down; the primary key is pressed and released;
        then modifiers are released in reverse order.
        """
        self._require_active_window()
        if not keys:
            return {"ok": False, "message": "hotkey: keys list is empty"}

        modifiers = [k for k in keys if is_modifier(k)]
        primaries = [k for k in keys if not is_modifier(k)]

        mod_keysyms = [named_key_to_keysym(m) for m in modifiers]
        primary_keysyms = [named_key_to_keysym(p) for p in primaries]

        await self._hold_modifiers(mod_keysyms)
        for ks in primary_keysyms:
            await self._send_keysym(ks)
        await self._release_modifiers(reversed(mod_keysyms))

        return {"ok": True, "message": f"hotkey {keys!r}"}

    async def set_value(
        self, window_id: int, element_index: int, value: str
    ) -> dict[str, Any]:
        """Set a text field value via AT-SPI EditableText (v2).

        Falls back to an error hint suggesting focus+type_text when AT-SPI is
        unavailable — preserving the v1 contract rather than silently failing.
        """
        try:
            await self._bridge.atspi_type(window_id, element_index, value)
            return {"ok": True, "message": f"set_value window={window_id} index={element_index}"}
        except (SessionBridgeError, SessionBridgeUnavailable) as exc:
            logger.debug("cua_driver.set_value.atspi_unavailable: %s", exc)
        return {
            "isError": True,
            "message": "set_value: AT-SPI unavailable — use click then type_text",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _click_by_element(
        self,
        window_id: int,
        element_index: int,
        button: int,
        count: int,
    ) -> dict[str, Any]:
        """Route a click through AT-SPI; fall back to coord click on bounds."""
        double = count >= 2
        try:
            resp = await self._bridge.atspi_click(
                window_id, element_index, double=double, button=_button_name(button)
            )
            # Bridge may return bounds when action iface is missing.
            bounds = resp.get("bounds")
            if bounds:
                cx, cy = _bounds_centre(bounds)
                for _ in range(count):
                    await self._bridge.pointer_motion(cx, cy)
                    await self._bridge.pointer_button(button, True)
                    await self._bridge.pointer_button(button, False)
                return {
                    "ok": True,
                    "message": (
                        f"click_element(w={window_id},i={element_index}) "
                        f"via coords ({cx},{cy}) ×{count}"
                    ),
                }
            return {
                "ok": True,
                "message": f"click_element(w={window_id},i={element_index}) ×{count}",
            }
        except (SessionBridgeError, SessionBridgeUnavailable) as exc:
            logger.debug(
                "cua_driver._click_by_element.atspi_unavailable: %s", exc
            )
        return {"ok": False, "message": "AT-SPI unavailable for element click"}

    def _require_active_window(self) -> None:
        if self._active_pid is None:
            raise NoActiveWindowError(
                "No active window — call capture() first to establish context."
            )

    async def _take_screenshot(self) -> dict[str, Any] | None:
        """Request a screenshot via the bridge and decode the raw file."""
        resp = await self._bridge.screenshot()
        path = resp.get("path")
        if not path:
            return None
        width = resp.get("width", _DEFAULT_WIDTH)
        height = resp.get("height", _DEFAULT_HEIGHT)
        raw_data = await asyncio.to_thread(_read_png_as_rgba, path, width, height)
        return {"data": raw_data, "width": width, "height": height}

    async def _send_keysym(self, keysym: int) -> None:
        # SessionBridgeClient does not yet expose a keysym verb directly;
        # type_text handles single chars via keysym internally in the bridge.
        # For named keys we add a keysym verb to the client (see bridge additions).
        await self._bridge.keysym(keysym, True)
        await self._bridge.keysym(keysym, False)

    async def _hold_modifiers(self, keysyms: list[int]) -> None:
        for ks in keysyms:
            await self._bridge.keysym(ks, True)

    async def _release_modifiers(self, keysyms) -> None:  # type: ignore[type-arg]
        for ks in keysyms:
            await self._bridge.keysym(ks, False)


# ------------------------------------------------------------------
# Module-level pure helpers
# ------------------------------------------------------------------

def _list_windows_v1_stub() -> dict[str, Any]:
    """Return the v1 fullscreen sentinel entry (degradation path)."""
    return {
        "windows": [
            {
                "app_name": "desktop",
                "pid": 0,
                "window_id": 0,
                "title": "LumenSO Desktop",
                "is_on_screen": True,
                "z_index": 0,
            }
        ]
    }


def _normalise_window_entry(w: dict[str, Any]) -> dict[str, Any]:
    """Map AT-SPI window dict to the MCP structuredContent.windows contract."""
    return {
        "app_name": w.get("app_name", ""),
        "pid": w.get("pid", 0),
        "window_id": w.get("window_id", 0),
        "title": w.get("title", ""),
        "is_on_screen": True,
        # Active window gets z_index=0 (front); inactive get incremental values.
        "z_index": 0 if w.get("is_active") else 1,
    }


def _format_window_tree(resp: dict[str, Any]) -> str:
    """Format bridge get_window_state response as a compact text summary.

    Example:
        Window: "Gedit" (3 elements)
        [0] push button "New"  @ (10,20,80,30)
        [1] entry ""  @ (100,20,400,30)
        [2] push button "Save"  @ (520,20,80,30)
    """
    title = resp.get("title", "")
    elements = resp.get("elements", [])
    lines = [f'Window: "{title}" ({len(elements)} elements)']
    for el in elements:
        bounds = el.get("bounds") or {}
        coord_str = (
            f"@ ({bounds.get('x',0)},{bounds.get('y',0)},"
            f"{bounds.get('w',0)},{bounds.get('h',0)})"
            if bounds else ""
        )
        lines.append(
            f'[{el["index"]}] {el["role"]} "{el["name"]}"  {coord_str}'.rstrip()
        )
    return "\n".join(lines)


def _button_name(button_index: int) -> str:
    return {0: "left", 1: "right", 2: "middle"}.get(button_index, "left")


def _bounds_centre(bounds: dict[str, Any]) -> tuple[float, float]:
    x = float(bounds.get("x", 0))
    y = float(bounds.get("y", 0))
    w = float(bounds.get("w", 0))
    h = float(bounds.get("h", 0))
    return x + w / 2.0, y + h / 2.0


def _scroll_to_axis_steps(direction: str, amount: int) -> tuple[int, int]:
    """Map a scroll direction to (axis, signed_steps).

    axis: 0=vertical, 1=horizontal (bridge convention).
    steps: positive=down/right, negative=up/left.
    """
    _MAP = {
        "up":    (0, -amount),
        "down":  (0, amount),
        "left":  (1, -amount),
        "right": (1, amount),
    }
    result = _MAP.get(direction.lower())
    if result is None:
        raise ValueError(f"Unknown scroll direction: {direction!r}")
    return result


def _encode_image(
    rgba: bytes, width: int, height: int, fmt: str, quality: int
) -> bytes:
    """Encode raw RGBA bytes to JPEG or PNG.

    Uses stdlib `zlib` for PNG (no Pillow required for PNG path).
    Uses Pillow for JPEG when available; falls back to PNG if not.
    """
    if fmt == "jpeg":
        return _encode_jpeg(rgba, width, height, quality)
    return _encode_png(rgba, width, height)


def _encode_jpeg(rgba: bytes, width: int, height: int, quality: int) -> bytes:
    try:
        from PIL import Image  # noqa: PLC0415
        img = Image.frombytes("RGBA", (width, height), rgba)
        rgb = img.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except ImportError:
        logger.warning("cua_driver.pillow_unavailable: falling back to PNG")
        return _encode_png(rgba, width, height)


def _encode_png(rgba: bytes, width: int, height: int) -> bytes:
    from hermes.shell_server.training.png_writer import encode_rgba_png  # noqa: PLC0415
    return encode_rgba_png(width, height, rgba)


def _read_png_as_rgba(path: str, width: int, height: int) -> bytes:
    """Read a PNG file written by the bridge and return raw RGBA bytes.

    Falls back to a blank RGBA buffer if the file cannot be read.
    """
    try:
        from PIL import Image  # noqa: PLC0415
        with Image.open(path) as img:
            rgba_img = img.convert("RGBA")
            w, h = rgba_img.size
            return rgba_img.tobytes()
    except (ImportError, OSError, Exception) as exc:  # noqa: BLE001
        logger.warning("cua_driver.read_png_failed path=%s: %s", path, exc)

    # Try minimal zlib PNG decode without Pillow.
    try:
        return _minimal_png_decode(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cua_driver.minimal_png_decode_failed: %s", exc)

    return bytes(width * height * 4)


def _minimal_png_decode(path: str) -> bytes:
    """Decode a simple RGBA PNG written by encode_rgba_png (filter=0 only)."""
    with open(path, "rb") as fh:
        data = fh.read()

    # Skip 8-byte signature.
    pos = 8
    width = height = 0
    idat_chunks: list[bytes] = []

    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk_data = data[pos + 8:pos + 8 + length]
        pos += 12 + length

        if tag == b"IHDR":
            width = struct.unpack(">I", chunk_data[0:4])[0]
            height = struct.unpack(">I", chunk_data[4:8])[0]
        elif tag == b"IDAT":
            idat_chunks.append(chunk_data)
        elif tag == b"IEND":
            break

    raw = zlib.decompress(b"".join(idat_chunks))
    row_bytes = width * 4
    rgba = bytearray()
    for y in range(height):
        start = y * (row_bytes + 1) + 1  # skip filter byte (always 0)
        rgba += raw[start:start + row_bytes]
    return bytes(rgba)


def _enumerate_proc_apps() -> list[dict[str, Any]]:
    """List running processes from /proc as name+pid pairs."""
    import os  # noqa: PLC0415
    apps: list[dict[str, Any]] = []
    try:
        for entry in os.scandir("/proc"):
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            pid = int(entry.name)
            comm_path = f"/proc/{pid}/comm"
            try:
                with open(comm_path, encoding="utf-8") as fh:
                    name = fh.read().strip()
                apps.append({"name": name, "pid": pid})
            except OSError:
                continue
    except OSError:
        pass
    return apps
