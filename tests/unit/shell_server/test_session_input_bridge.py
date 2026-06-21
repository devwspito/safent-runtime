"""Unit tests for SessionInputBridge — session-side host-operation helper.

Tests cover:
  - Token verification (auth gate)
  - Rate limiting
  - Key-chord denylist (Ctrl-Alt-Fx)
  - InputOwnership contention guard (OPERATOR blocks AGENT)
  - Verb dispatch happy paths (mock mirror + FakeScreenCaptureBackend)
  - Screenshot verb uses injected capture_backend (no mutter/GStreamer in test)
  - type_text keysym synthesis
  - Framing helpers (_read_frame / _send_frame round-trip)

No real mutter / D-Bus / GStreamer is required.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from hermes.agents_os.application.teaching.input_ownership_ledger import InputOwnershipLedger
from hermes.agents_os.application.teaching.teaching_context import InputOwner
from hermes.shell_server.screen_capture.fake import FakeScreenCaptureBackend
from hermes.shell_server.session_agent.input_bridge import (
    BridgeOwnershipError,
    BridgeRateLimitError,
    MAX_REQUESTS_PER_SECOND,
    SessionInputBridge,
    _OWNERSHIP_CONTEXT_ID,
    _char_to_keysym,
    _read_frame,
    _send_frame,
    _verify_token,
)

pytestmark = pytest.mark.unit

_TOKEN = "deadbeefdeadbeefdeadbeef"
_DAEMON_UID = 1001


def _make_bridge(
    *,
    ledger: InputOwnershipLedger | None = None,
    capture_backend=None,
) -> tuple[SessionInputBridge, MagicMock]:
    mirror = MagicMock()
    mirror.pointer_motion = MagicMock(return_value=None)
    mirror.pointer_button = MagicMock(return_value=None)
    mirror.pointer_axis_discrete = MagicMock(return_value=None)
    mirror.keyboard_keycode = MagicMock(return_value=None)
    mirror.keyboard_keysym = MagicMock(return_value=None)
    ledger = ledger or InputOwnershipLedger()
    backend = capture_backend if capture_backend is not None else FakeScreenCaptureBackend()
    bridge = SessionInputBridge(
        token=_TOKEN,
        ledger=ledger,
        mirror=mirror,
        capture_backend=backend,
        daemon_uid=_DAEMON_UID,
    )
    return bridge, mirror


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


class TestTokenVerification:
    def test_correct_token_passes(self) -> None:
        assert _verify_token(_TOKEN, _TOKEN) is True

    def test_wrong_token_fails(self) -> None:
        assert _verify_token("wrong", _TOKEN) is False

    def test_empty_token_fails(self) -> None:
        assert _verify_token("", _TOKEN) is False

    def test_prefix_of_token_fails(self) -> None:
        assert _verify_token(_TOKEN[:8], _TOKEN) is False


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_within_limit_returns_true(self) -> None:
        bridge, _ = _make_bridge()
        for _ in range(MAX_REQUESTS_PER_SECOND):
            assert bridge._check_rate_limit() is True

    def test_exceeding_limit_returns_false(self) -> None:
        bridge, _ = _make_bridge()
        for _ in range(MAX_REQUESTS_PER_SECOND):
            bridge._check_rate_limit()
        assert bridge._check_rate_limit() is False

    def test_window_resets_after_one_second(self) -> None:
        import time
        bridge, _ = _make_bridge()
        for _ in range(MAX_REQUESTS_PER_SECOND):
            bridge._check_rate_limit()
        # Simulate window expiry by back-dating the window start.
        bridge._window_start -= 1.1
        assert bridge._check_rate_limit() is True


# ---------------------------------------------------------------------------
# Key-chord denylist
# ---------------------------------------------------------------------------


class TestChordDenylist:
    def test_ctrl_alt_f1_denied(self) -> None:
        bridge, _ = _make_bridge()
        bridge._pressed_ctrl = True
        bridge._pressed_alt = True
        assert bridge._is_denied_chord(59) is True  # F1

    def test_ctrl_alt_f12_denied(self) -> None:
        bridge, _ = _make_bridge()
        bridge._pressed_ctrl = True
        bridge._pressed_alt = True
        assert bridge._is_denied_chord(88) is True  # F12

    def test_ctrl_alt_delete_denied(self) -> None:
        bridge, _ = _make_bridge()
        bridge._pressed_ctrl = True
        bridge._pressed_alt = True
        assert bridge._is_denied_chord(111) is True  # Delete

    def test_without_ctrl_not_denied(self) -> None:
        bridge, _ = _make_bridge()
        bridge._pressed_ctrl = False
        bridge._pressed_alt = True
        assert bridge._is_denied_chord(59) is False

    def test_without_alt_not_denied(self) -> None:
        bridge, _ = _make_bridge()
        bridge._pressed_ctrl = True
        bridge._pressed_alt = False
        assert bridge._is_denied_chord(59) is False

    def test_regular_key_not_denied(self) -> None:
        bridge, _ = _make_bridge()
        bridge._pressed_ctrl = True
        bridge._pressed_alt = True
        assert bridge._is_denied_chord(30) is False  # 'a' key

    def test_modifier_state_updates_on_press(self) -> None:
        bridge, _ = _make_bridge()
        bridge._update_modifier_state(29, True)   # LeftCtrl press
        assert bridge._pressed_ctrl is True
        bridge._update_modifier_state(29, False)  # LeftCtrl release
        assert bridge._pressed_ctrl is False

    def test_right_ctrl_also_tracked(self) -> None:
        bridge, _ = _make_bridge()
        bridge._update_modifier_state(97, True)  # RightCtrl
        assert bridge._pressed_ctrl is True


# ---------------------------------------------------------------------------
# InputOwnership contention guard
# ---------------------------------------------------------------------------


class TestOwnershipGuard:
    def test_agent_blocked_when_operator_owns(self) -> None:
        ledger = InputOwnershipLedger()
        ledger.claim(_OWNERSHIP_CONTEXT_ID, InputOwner.OPERATOR)
        bridge, _ = _make_bridge(ledger=ledger)
        with pytest.raises(BridgeOwnershipError):
            bridge._assert_agent_owns_input()

    def test_agent_allowed_when_context_unclaimed(self) -> None:
        bridge, _ = _make_bridge()
        # Should not raise — no owner claimed
        bridge._assert_agent_owns_input()

    def test_agent_allowed_when_agent_owns(self) -> None:
        ledger = InputOwnershipLedger()
        ledger.claim(_OWNERSHIP_CONTEXT_ID, InputOwner.AGENT)
        bridge, _ = _make_bridge(ledger=ledger)
        # Should not raise — agent is the owner
        bridge._assert_agent_owns_input()


# ---------------------------------------------------------------------------
# Dispatch — happy path (mocked mirror)
# ---------------------------------------------------------------------------


class TestScreenshot:
    """Screenshot verb uses the injected ScreenCaptureBackend — no mutter/GStreamer."""

    async def test_screenshot_uses_injected_backend(self, tmp_path) -> None:
        """FakeScreenCaptureBackend delivers a real frame; bridge must write a PNG.

        _do_screenshot imports pathlib.Path locally as 'P', so we patch
        pathlib.Path in the pathlib module — the standard approach for
        functions that import names from stdlib at call time.
        """
        import pathlib
        from unittest.mock import patch as _patch

        orig_path = pathlib.Path

        def _path_factory(p):
            if str(p) == "/var/lib/hermes/os-skills":
                return tmp_path
            return orig_path(p)

        backend = FakeScreenCaptureBackend(width=4, height=4, frames=1)
        bridge, _ = _make_bridge(capture_backend=backend)

        with _patch("pathlib.Path", side_effect=_path_factory):
            result = await bridge._handle_screenshot({})

        assert result["ok"] is True, result
        assert "path" in result
        assert result["width"] == 4
        assert result["height"] == 4

    async def test_screenshot_error_on_blank_backend(self) -> None:
        """A backend that yields only blank frames must return ok=False."""
        from hermes.shell_server.screen_capture.domain import CaptureTarget, FrameCallback

        class BlankBackend:
            def start(self, target: CaptureTarget, on_frame: FrameCallback) -> None:
                pass  # emits nothing

            def latest_frame(self):
                return None

            def stop(self) -> None:
                pass

        bridge, _ = _make_bridge(capture_backend=BlankBackend())
        result = await bridge._handle_screenshot({})
        assert result["ok"] is False
        assert "error" in result


class TestDispatch:
    async def test_pointer_motion_calls_mirror(self) -> None:
        bridge, mirror = _make_bridge()
        result = await bridge._handle_pointer_motion({"x": 100.0, "y": 200.0})
        assert result == {"ok": True}
        mirror.pointer_motion.assert_called_once_with(100.0, 200.0)

    async def test_pointer_button_calls_mirror(self) -> None:
        bridge, mirror = _make_bridge()
        result = await bridge._handle_pointer_button({"btn": 0, "press": True})
        assert result == {"ok": True}
        # BTN_LEFT = 0x110 = 272
        mirror.pointer_button.assert_called_once_with(272, True)

    async def test_keycode_calls_mirror(self) -> None:
        bridge, mirror = _make_bridge()
        result = await bridge._handle_keycode({"code": 30, "press": True})
        assert result == {"ok": True}
        mirror.keyboard_keycode.assert_called_once_with(30, True)

    async def test_keycode_denied_chord_returns_error(self) -> None:
        bridge, mirror = _make_bridge()
        bridge._pressed_ctrl = True
        bridge._pressed_alt = True
        result = await bridge._handle_keycode({"code": 59, "press": True})  # F1
        assert result["ok"] is False
        assert "chord_denied" in result["error"]
        mirror.keyboard_keycode.assert_not_called()

    async def test_type_text_calls_keysym_per_char(self) -> None:
        bridge, mirror = _make_bridge()
        result = await bridge._handle_type_text({"text": "hi"})
        assert result == {"ok": True}
        # 'h'=104, 'i'=105 — press + release for each
        assert mirror.keyboard_keysym.call_count == 4

    async def test_type_text_too_long_rejected(self) -> None:
        bridge, _ = _make_bridge()
        result = await bridge._handle_type_text({"text": "x" * 4097})
        assert result["ok"] is False
        assert "too_long" in result["error"]

    async def test_dispatch_unknown_verb_returns_error(self) -> None:
        bridge, _ = _make_bridge()
        result = await bridge._dispatch({"token": _TOKEN, "verb": "fly_to_moon"})
        assert result["ok"] is False

    async def test_dispatch_bad_token_raises(self) -> None:
        bridge, _ = _make_bridge()
        import pytest as _pytest
        from hermes.shell_server.session_agent.input_bridge import BridgeAuthError
        with _pytest.raises(BridgeAuthError):
            await bridge._dispatch({"token": "bad", "verb": "screenshot"})


# ---------------------------------------------------------------------------
# type_text keysym helper
# ---------------------------------------------------------------------------


class TestCharToKeysym:
    def test_ascii_space(self) -> None:
        assert _char_to_keysym(" ") == 0x20

    def test_ascii_a(self) -> None:
        assert _char_to_keysym("a") == ord("a")

    def test_ascii_tilde(self) -> None:
        assert _char_to_keysym("~") == 0x7e

    def test_unicode_beyond_ascii(self) -> None:
        # U+00E9 'é' → 0x01000000 | 0xE9 = 0x010000E9
        assert _char_to_keysym("é") == 0x010000E9

    def test_emoji_high_codepoint(self) -> None:
        cp = ord("😀")
        assert _char_to_keysym("😀") == 0x01000000 | cp


# ---------------------------------------------------------------------------
# Framing round-trip
# ---------------------------------------------------------------------------


class TestFraming:
    async def test_roundtrip_simple_dict(self) -> None:
        payload = {"verb": "screenshot", "token": "abc"}
        buf = io.BytesIO()

        class _FakeWriter:
            def write(self, data: bytes) -> None:
                buf.write(data)
            async def drain(self) -> None:
                pass

        writer = _FakeWriter()
        await _send_frame(writer, payload)  # type: ignore[arg-type]

        buf.seek(0)
        reader = asyncio.StreamReader()
        reader.feed_data(buf.read())
        reader.feed_eof()

        result = await _read_frame(reader)
        assert result == payload

    async def test_read_frame_returns_none_on_eof(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_eof()
        result = await _read_frame(reader)
        assert result is None
