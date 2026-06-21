"""Unit tests for CuaActionExecutor — action→bridge mapping.

All bridge I/O is replaced by a FakeBridgeClient that records calls.
No real socket, no compositor, no PySide6 required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from hermes.capabilities.infrastructure.session_bridge_client import SessionBridgeError
from hermes.lumen.cua_driver._action_executor import (
    CuaActionExecutor,
    NoActiveWindowError,
    _bounds_centre,
    _button_name,
    _encode_png,
    _format_window_tree,
    _list_windows_v1_stub,
    _normalise_window_entry,
    _scroll_to_axis_steps,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake bridge client
# ---------------------------------------------------------------------------

class FakeBridgeClient:
    """Records every call for assertion in tests."""

    def __init__(
        self,
        *,
        screenshot_response: dict[str, Any] | None = None,
        list_windows_response: dict[str, Any] | None = None,
        get_window_state_response: dict[str, Any] | None = None,
        atspi_click_response: dict[str, Any] | None = None,
        atspi_type_response: dict[str, Any] | None = None,
        atspi_unavailable: bool = False,
    ) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._screenshot_response = screenshot_response or {
            "ok": True,
            "path": "/tmp/fake_screen.png",
            "width": 8,
            "height": 8,
        }
        self._list_windows_response = list_windows_response
        self._get_window_state_response = get_window_state_response
        self._atspi_click_response = atspi_click_response or {"ok": True}
        self._atspi_type_response = atspi_type_response or {"ok": True}
        self._atspi_unavailable = atspi_unavailable

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def _maybe_raise(self) -> None:
        if self._atspi_unavailable:
            raise SessionBridgeError("atspi_unavailable")

    async def screenshot(self) -> dict[str, Any]:
        self._record("screenshot")
        return self._screenshot_response

    async def pointer_motion(self, x: float, y: float) -> dict[str, Any]:
        self._record("pointer_motion", x, y)
        return {"ok": True}

    async def pointer_button(self, btn: int, press: bool) -> dict[str, Any]:
        self._record("pointer_button", btn, press)
        return {"ok": True}

    async def pointer_axis(self, axis: int, steps: int) -> dict[str, Any]:
        self._record("pointer_axis", axis, steps)
        return {"ok": True}

    async def keysym(self, sym: int, press: bool) -> dict[str, Any]:
        self._record("keysym", sym, press)
        return {"ok": True}

    async def type_text(self, text: str) -> dict[str, Any]:
        self._record("type_text", text)
        return {"ok": True}

    async def keycode(self, code: int, press: bool) -> dict[str, Any]:
        self._record("keycode", code, press)
        return {"ok": True}

    async def list_windows(self) -> dict[str, Any]:
        self._record("list_windows")
        self._maybe_raise()
        if self._list_windows_response is not None:
            return self._list_windows_response
        return {
            "ok": True,
            "windows": [
                {
                    "app_name": "gedit",
                    "pid": 1234,
                    "window_id": 0,
                    "title": "Untitled - gedit",
                    "is_active": True,
                    "bounds": {"x": 0, "y": 0, "w": 800, "h": 600},
                }
            ],
        }

    async def get_window_state(self, window_id: int) -> dict[str, Any]:
        self._record("get_window_state", window_id)
        self._maybe_raise()
        if self._get_window_state_response is not None:
            return self._get_window_state_response
        return {
            "ok": True,
            "title": "Untitled - gedit",
            "elements": [
                {"index": 0, "role": "push button", "name": "Save",
                 "bounds": {"x": 10, "y": 10, "w": 80, "h": 30}},
                {"index": 1, "role": "entry", "name": "",
                 "bounds": {"x": 100, "y": 10, "w": 600, "h": 30}},
            ],
        }

    async def atspi_click(
        self,
        window_id: int,
        element_index: int,
        double: bool = False,
        button: str = "left",
    ) -> dict[str, Any]:
        self._record("atspi_click", window_id, element_index, double=double, button=button)
        self._maybe_raise()
        return self._atspi_click_response

    async def atspi_type(
        self,
        window_id: int,
        element_index: int,
        text: str,
    ) -> dict[str, Any]:
        self._record("atspi_type", window_id, element_index, text)
        self._maybe_raise()
        return self._atspi_type_response


def _fake_rgba(width: int = 8, height: int = 8) -> bytes:
    return bytes(width * height * 4)


def _make_executor_with_active_window(
    bridge: FakeBridgeClient | None = None,
) -> CuaActionExecutor:
    """Return an executor with an active window already set."""
    if bridge is None:
        bridge = FakeBridgeClient()
    exe = CuaActionExecutor(bridge)
    exe._active_pid = 0
    exe._active_window_id = 0
    exe._screen_width = 8
    exe._screen_height = 8
    return exe


# ---------------------------------------------------------------------------
# NoActiveWindowError guard
# ---------------------------------------------------------------------------

class TestNoActiveWindowGuard:
    async def test_click_without_capture_raises(self) -> None:
        exe = CuaActionExecutor(FakeBridgeClient())
        with pytest.raises(NoActiveWindowError, match="capture"):
            await exe.click(10.0, 20.0)

    async def test_type_text_without_capture_raises(self) -> None:
        exe = CuaActionExecutor(FakeBridgeClient())
        with pytest.raises(NoActiveWindowError):
            await exe.type_text("hello")

    async def test_scroll_without_capture_raises(self) -> None:
        exe = CuaActionExecutor(FakeBridgeClient())
        with pytest.raises(NoActiveWindowError):
            await exe.scroll("down", 3)

    async def test_press_key_without_capture_raises(self) -> None:
        exe = CuaActionExecutor(FakeBridgeClient())
        with pytest.raises(NoActiveWindowError):
            await exe.press_key("return")

    async def test_hotkey_without_capture_raises(self) -> None:
        exe = CuaActionExecutor(FakeBridgeClient())
        with pytest.raises(NoActiveWindowError):
            await exe.hotkey(["ctrl", "c"])


# ---------------------------------------------------------------------------
# Click mapping
# ---------------------------------------------------------------------------

class TestClick:
    async def test_single_click_sends_motion_then_down_up(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.click(100.0, 200.0)

        verbs = [c[0] for c in bridge.calls]
        assert verbs == ["pointer_motion", "pointer_button", "pointer_button"]
        assert bridge.calls[0] == ("pointer_motion", (100.0, 200.0), {})
        assert bridge.calls[1] == ("pointer_button", (0, True), {})
        assert bridge.calls[2] == ("pointer_button", (0, False), {})

    async def test_double_click_sends_two_cycles(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.click(10.0, 10.0, count=2)

        button_calls = [c for c in bridge.calls if c[0] == "pointer_button"]
        assert len(button_calls) == 4  # 2 × (down + up)

    async def test_right_click_uses_button_1(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.click(0.0, 0.0, button=1)

        btn_calls = [c for c in bridge.calls if c[0] == "pointer_button"]
        assert btn_calls[0][1][0] == 1  # button index 1 = right


# ---------------------------------------------------------------------------
# Drag mapping
# ---------------------------------------------------------------------------

class TestDrag:
    async def test_drag_starts_with_motion_button_down(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.drag(0.0, 0.0, 100.0, 100.0)

        first_call = bridge.calls[0]
        assert first_call[0] == "pointer_motion"
        assert first_call[1] == (0.0, 0.0)

        second_call = bridge.calls[1]
        assert second_call == ("pointer_button", (0, True), {})

    async def test_drag_ends_with_button_up(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.drag(0.0, 0.0, 100.0, 100.0)

        last_call = bridge.calls[-1]
        assert last_call == ("pointer_button", (0, False), {})

    async def test_drag_interpolates_intermediate_steps(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.drag(0.0, 0.0, 100.0, 100.0)

        motion_calls = [c for c in bridge.calls if c[0] == "pointer_motion"]
        # 1 (initial) + _DRAG_STEPS (20) interpolations
        assert len(motion_calls) == 21


# ---------------------------------------------------------------------------
# Scroll mapping
# ---------------------------------------------------------------------------

class TestScroll:
    async def test_scroll_down_uses_vertical_axis_positive(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.scroll("down", 3)

        axis_call = next(c for c in bridge.calls if c[0] == "pointer_axis")
        assert axis_call[1] == (0, 3)  # axis=0 (vertical), steps=+3

    async def test_scroll_up_is_negative(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.scroll("up", 5)

        axis_call = next(c for c in bridge.calls if c[0] == "pointer_axis")
        assert axis_call[1] == (0, -5)

    async def test_scroll_right_uses_horizontal_axis(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.scroll("right", 2)

        axis_call = next(c for c in bridge.calls if c[0] == "pointer_axis")
        assert axis_call[1] == (1, 2)

    async def test_scroll_with_position_sends_motion_first(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.scroll("down", 1, x=50.0, y=50.0)

        assert bridge.calls[0][0] == "pointer_motion"
        assert bridge.calls[1][0] == "pointer_axis"

    def test_scroll_axis_steps_pure_function(self) -> None:
        assert _scroll_to_axis_steps("up", 3) == (0, -3)
        assert _scroll_to_axis_steps("down", 3) == (0, 3)
        assert _scroll_to_axis_steps("left", 2) == (1, -2)
        assert _scroll_to_axis_steps("right", 2) == (1, 2)

    def test_unknown_direction_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown scroll direction"):
            _scroll_to_axis_steps("diagonal", 1)


# ---------------------------------------------------------------------------
# Type text mapping
# ---------------------------------------------------------------------------

class TestTypeText:
    async def test_type_text_delegates_to_bridge(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.type_text("hello")

        type_calls = [c for c in bridge.calls if c[0] == "type_text"]
        assert len(type_calls) == 1
        assert type_calls[0][1][0] == "hello"


# ---------------------------------------------------------------------------
# Press key / hotkey mapping
# ---------------------------------------------------------------------------

class TestPressKey:
    async def test_press_key_return_sends_keysym_down_up(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.press_key("return")

        keysym_calls = [c for c in bridge.calls if c[0] == "keysym"]
        assert len(keysym_calls) == 2
        # Return keysym = 0xFF0D
        assert keysym_calls[0][1] == (0xFF0D, True)
        assert keysym_calls[1][1] == (0xFF0D, False)

    async def test_press_key_unknown_raises_value_error(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        with pytest.raises((ValueError, NoActiveWindowError)):
            await exe.press_key("not_a_key_xyz")

    async def test_hotkey_holds_modifier_then_releases(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        await exe.hotkey(["ctrl", "c"])

        keysym_calls = [c for c in bridge.calls if c[0] == "keysym"]
        # Sequence: ctrl↓, c↓, c↑, ctrl↑
        assert len(keysym_calls) == 4
        ctrl_ks = 0xFFE3
        c_ks = ord("c")
        assert keysym_calls[0] == ("keysym", (ctrl_ks, True), {})
        assert keysym_calls[1] == ("keysym", (c_ks, True), {})
        assert keysym_calls[2] == ("keysym", (c_ks, False), {})
        assert keysym_calls[3] == ("keysym", (ctrl_ks, False), {})

    async def test_hotkey_empty_keys_returns_error(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        result = await exe.hotkey([])
        assert result.get("ok") is False


# ---------------------------------------------------------------------------
# List windows (v2 AT-SPI + v1 fallback)
# ---------------------------------------------------------------------------

class TestListWindowsApps:
    async def test_list_windows_v2_returns_atspi_windows(self) -> None:
        bridge = FakeBridgeClient()
        exe = CuaActionExecutor(bridge)
        result = await exe.list_windows()
        assert len(result["windows"]) == 1
        w = result["windows"][0]
        assert w["app_name"] == "gedit"
        assert w["pid"] == 1234
        assert w["window_id"] == 0
        assert "list_windows" in [c[0] for c in bridge.calls]

    async def test_list_windows_falls_back_to_v1_on_atspi_error(self) -> None:
        bridge = FakeBridgeClient(atspi_unavailable=True)
        exe = CuaActionExecutor(bridge)
        result = await exe.list_windows()
        # Should degrade to the sentinel desktop entry (v1 stub).
        assert len(result["windows"]) == 1
        assert result["windows"][0]["app_name"] == "desktop"

    async def test_list_apps_returns_list(self) -> None:
        exe = CuaActionExecutor(FakeBridgeClient())
        result = await exe.list_apps()
        assert "apps" in result
        assert isinstance(result["apps"], list)

    async def test_get_window_state_v2_returns_tree_text(self) -> None:
        bridge = FakeBridgeClient()
        exe = CuaActionExecutor(bridge)
        text, img = await exe.get_window_state(pid=1234, window_id=0)
        assert "gedit" in text
        assert "[0]" in text
        assert img is None

    async def test_get_window_state_falls_back_to_v1_stub(self) -> None:
        bridge = FakeBridgeClient(atspi_unavailable=True)
        exe = CuaActionExecutor(bridge)
        text, img = await exe.get_window_state(pid=1234, window_id=0)
        # v1 fallback uses AXWindow sentinel.
        assert "AXWindow" in text
        assert img is None

    async def test_get_window_state_without_window_id_uses_v1(self) -> None:
        exe = CuaActionExecutor(FakeBridgeClient())
        text, img = await exe.get_window_state(pid=999, window_id=None)
        assert "AXWindow" in text
        assert img is None


# ---------------------------------------------------------------------------
# Set value (v2 AT-SPI + v1 fallback)
# ---------------------------------------------------------------------------

class TestSetValue:
    async def test_set_value_calls_atspi_type(self) -> None:
        bridge = FakeBridgeClient()
        exe = CuaActionExecutor(bridge)
        result = await exe.set_value(window_id=0, element_index=1, value="hello")
        assert result["ok"] is True
        atspi_calls = [c for c in bridge.calls if c[0] == "atspi_type"]
        assert len(atspi_calls) == 1
        assert atspi_calls[0][1] == (0, 1, "hello")

    async def test_set_value_falls_back_to_error_when_atspi_unavailable(self) -> None:
        bridge = FakeBridgeClient(atspi_unavailable=True)
        exe = CuaActionExecutor(bridge)
        result = await exe.set_value(window_id=0, element_index=0, value="test")
        assert result["isError"] is True


# ---------------------------------------------------------------------------
# Capture / screenshot integration
# ---------------------------------------------------------------------------

class TestCapture:
    async def test_capture_sets_active_window(self) -> None:
        bridge = FakeBridgeClient()
        exe = CuaActionExecutor(bridge)
        assert exe._active_pid is None

        # Patch _take_screenshot to avoid filesystem access
        async def _fake_take() -> dict:
            return {"data": _fake_rgba(8, 8), "width": 8, "height": 8}

        exe._take_screenshot = _fake_take  # type: ignore[method-assign]
        await exe.capture()

        assert exe._active_pid == 0

    async def test_capture_returns_image_content_part(self) -> None:
        bridge = FakeBridgeClient()
        exe = CuaActionExecutor(bridge)

        async def _fake_take() -> dict:
            return {"data": _fake_rgba(8, 8), "width": 8, "height": 8}

        exe._take_screenshot = _fake_take  # type: ignore[method-assign]
        result = await exe.capture(fmt="png")

        assert result.get("type") == "image"
        assert result.get("mimeType") == "image/png"
        assert isinstance(result.get("data"), str)

    async def test_capture_after_active_window_set_allows_click(self) -> None:
        bridge = FakeBridgeClient()
        exe = CuaActionExecutor(bridge)

        async def _fake_take() -> dict:
            return {"data": _fake_rgba(8, 8), "width": 8, "height": 8}

        exe._take_screenshot = _fake_take  # type: ignore[method-assign]
        await exe.capture()

        # Should not raise now
        result = await exe.click(1.0, 1.0)
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# AT-SPI click via element_index (v2)
# ---------------------------------------------------------------------------

class TestClickByElement:
    async def test_click_by_element_calls_atspi_click(self) -> None:
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        result = await exe.click(0.0, 0.0, window_id=0, element_index=1)
        assert result["ok"] is True
        atspi = [c for c in bridge.calls if c[0] == "atspi_click"]
        assert len(atspi) == 1
        assert atspi[0][1][:2] == (0, 1)

    async def test_click_by_element_with_bounds_fallback_uses_pointer(self) -> None:
        """When bridge returns bounds, executor performs pointer click at centre."""
        bounds_resp = {"ok": True, "bounds": {"x": 100, "y": 200, "w": 80, "h": 30}}
        bridge = FakeBridgeClient(atspi_click_response=bounds_resp)
        exe = _make_executor_with_active_window(bridge)
        result = await exe.click(0.0, 0.0, window_id=0, element_index=0)
        assert result["ok"] is True
        motion_calls = [c for c in bridge.calls if c[0] == "pointer_motion"]
        assert len(motion_calls) == 1
        # Centre: x=100+40=140, y=200+15=215
        assert motion_calls[0][1] == (140.0, 215.0)

    async def test_click_by_element_atspi_unavailable_returns_error(self) -> None:
        bridge = FakeBridgeClient(atspi_unavailable=True)
        exe = _make_executor_with_active_window(bridge)
        result = await exe.click(0.0, 0.0, window_id=0, element_index=0)
        assert result["ok"] is False

    async def test_click_by_coords_unchanged_when_no_element_index(self) -> None:
        """v1 coord path must be unaffected by the element_index parameter."""
        bridge = FakeBridgeClient()
        exe = _make_executor_with_active_window(bridge)
        result = await exe.click(50.0, 60.0)
        assert result["ok"] is True
        motion_calls = [c for c in bridge.calls if c[0] == "pointer_motion"]
        assert motion_calls[0][1] == (50.0, 60.0)


# ---------------------------------------------------------------------------
# Pure helper functions (v2)
# ---------------------------------------------------------------------------

class TestPureHelpers:
    def test_bounds_centre(self) -> None:
        assert _bounds_centre({"x": 100, "y": 200, "w": 80, "h": 30}) == (140.0, 215.0)
        assert _bounds_centre({"x": 0, "y": 0, "w": 0, "h": 0}) == (0.0, 0.0)

    def test_button_name(self) -> None:
        assert _button_name(0) == "left"
        assert _button_name(1) == "right"
        assert _button_name(2) == "middle"
        assert _button_name(99) == "left"  # unknown → left

    def test_list_windows_v1_stub_structure(self) -> None:
        result = _list_windows_v1_stub()
        assert len(result["windows"]) == 1
        w = result["windows"][0]
        assert w["app_name"] == "desktop"
        assert w["is_on_screen"] is True

    def test_normalise_window_entry_active(self) -> None:
        entry = {
            "app_name": "firefox",
            "pid": 555,
            "window_id": 2,
            "title": "Firefox",
            "is_active": True,
        }
        result = _normalise_window_entry(entry)
        assert result["z_index"] == 0
        assert result["is_on_screen"] is True

    def test_normalise_window_entry_inactive(self) -> None:
        entry = {"app_name": "t", "pid": 0, "window_id": 0, "title": "", "is_active": False}
        result = _normalise_window_entry(entry)
        assert result["z_index"] == 1

    def test_format_window_tree_includes_elements(self) -> None:
        resp = {
            "title": "Test App",
            "elements": [
                {"index": 0, "role": "push button", "name": "OK",
                 "bounds": {"x": 10, "y": 20, "w": 80, "h": 30}},
                {"index": 1, "role": "entry", "name": "",
                 "bounds": {}},
            ],
        }
        text = _format_window_tree(resp)
        assert 'Window: "Test App" (2 elements)' in text
        assert "[0] push button" in text
        assert "OK" in text
        assert "[1] entry" in text

    def test_format_window_tree_empty(self) -> None:
        text = _format_window_tree({"title": "Empty", "elements": []})
        assert "0 elements" in text


# ---------------------------------------------------------------------------
# AT-SPI bridge verb tests (SessionInputBridge extension)
# ---------------------------------------------------------------------------

class TestBridgeAtSpiHandlers:
    """Tests for the new AT-SPI verbs added to SessionInputBridge.

    The bridge is instantiated in isolation using its internal handler methods
    — same approach as the existing TestDispatch suite in test_session_input_bridge.py.
    We mock _get_atspi_client so pyatspi is not needed in CI.
    """
    from unittest.mock import MagicMock

    def _make_bridge_with_fake_atspi(self, client_mock):
        from unittest.mock import MagicMock
        from hermes.agents_os.application.teaching.input_ownership_ledger import (
            InputOwnershipLedger,
        )
        from hermes.shell_server.screen_capture.fake import FakeScreenCaptureBackend
        from hermes.shell_server.session_agent.input_bridge import SessionInputBridge

        mirror = MagicMock()
        bridge = SessionInputBridge(
            token="tok",
            ledger=InputOwnershipLedger(),
            mirror=mirror,
            capture_backend=FakeScreenCaptureBackend(),
            daemon_uid=999,
        )
        bridge._atspi_client = client_mock
        return bridge

    def _fake_atspi(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.list_windows.return_value = [
            {"app_name": "gedit", "pid": 1, "window_id": 0,
             "title": "Untitled", "is_active": True, "bounds": {}}
        ]
        client.get_window_tree.return_value = {
            "title": "Untitled",
            "elements": [{"index": 0, "role": "push button", "name": "Save", "bounds": {}}],
        }
        client.click_element.return_value = None
        client.set_text_element.return_value = True
        return client

    async def test_list_windows_handler(self) -> None:
        from hermes.shell_server.session_agent.input_bridge import SessionInputBridge
        client = self._fake_atspi()
        bridge = self._make_bridge_with_fake_atspi(client)
        result = await bridge._handle_list_windows({})
        assert result["ok"] is True
        assert len(result["windows"]) == 1
        client.list_windows.assert_called_once()

    async def test_get_window_state_handler(self) -> None:
        client = self._fake_atspi()
        bridge = self._make_bridge_with_fake_atspi(client)
        result = await bridge._handle_get_window_state({"window_id": 0})
        assert result["ok"] is True
        assert result["title"] == "Untitled"
        assert len(result["elements"]) == 1

    async def test_atspi_click_handler_no_fallback(self) -> None:
        client = self._fake_atspi()
        bridge = self._make_bridge_with_fake_atspi(client)
        result = await bridge._handle_atspi_click(
            {"window_id": 0, "element_index": 0, "double": False, "button": "left"}
        )
        assert result["ok"] is True
        assert "bounds" not in result

    async def test_atspi_click_handler_with_bounds_fallback(self) -> None:
        client = self._fake_atspi()
        client.click_element.return_value = {"bounds": {"x": 10, "y": 20, "w": 80, "h": 30}}
        bridge = self._make_bridge_with_fake_atspi(client)
        result = await bridge._handle_atspi_click(
            {"window_id": 0, "element_index": 0}
        )
        assert result["ok"] is True
        assert result["bounds"] == {"x": 10, "y": 20, "w": 80, "h": 30}

    async def test_atspi_type_handler_success(self) -> None:
        client = self._fake_atspi()
        bridge = self._make_bridge_with_fake_atspi(client)
        result = await bridge._handle_atspi_type(
            {"window_id": 0, "element_index": 1, "text": "hello"}
        )
        assert result["ok"] is True
        client.set_text_element.assert_called_once_with(0, 1, "hello")

    async def test_atspi_type_handler_failure(self) -> None:
        client = self._fake_atspi()
        client.set_text_element.return_value = False
        bridge = self._make_bridge_with_fake_atspi(client)
        result = await bridge._handle_atspi_type(
            {"window_id": 0, "element_index": 0, "text": "x"}
        )
        assert result["ok"] is False
        assert "atspi_set_text_failed" in result["error"]

    async def test_list_windows_handler_no_atspi_returns_error(self) -> None:
        bridge_no_atspi = self._make_bridge_with_fake_atspi(False)  # False = unavailable
        result = await bridge_no_atspi._handle_list_windows({})
        assert result["ok"] is False
        assert "atspi_unavailable" in result["error"]
