"""Unit tests for CdpInputAdapter — mock CDP session, no real browser.

Coverage:
  - _keysym_to_char: ASCII range, Unicode extension range, non-printable.
  - pointer_motion: fires Input.dispatchMouseEvent mouseMoved.
  - pointer_button: press and release map to correct CDP button names.
  - pointer_axis_discrete: vertical and horizontal scroll deltas.
  - keyboard_keysym: printable chars emit 'char' event; release is no-op.
  - keyboard_keysym: non-printable keysym → no CDP send.
  - keyboard_keycode: logs warning, no CDP send.
  - async_mouse_click: press then release with correct params.
  - async_type_text: one 'char' event per character.
  - stop() is a no-op (does not detach session).
  - SeatInputEffectorPort structural compliance.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from hermes.browser.infrastructure.cdp_input_adapter import (
    CdpInputAdapter,
    _keysym_to_char,
)
from hermes.shell_server.mirror.input_effector_port import SeatInputEffectorPort


# ---------------------------------------------------------------------------
# _keysym_to_char — pure helper
# ---------------------------------------------------------------------------


class TestKeysymToChar:
    def test_ascii_printable_range(self) -> None:
        assert _keysym_to_char(ord("A")) == "A"
        assert _keysym_to_char(ord(" ")) == " "
        assert _keysym_to_char(ord("~")) == "~"

    def test_unicode_extension(self) -> None:
        # keysym = 0x01000000 | codepoint
        char = _keysym_to_char(0x01000000 | ord("€"))
        assert char == "€"

    def test_below_printable_range_returns_none(self) -> None:
        assert _keysym_to_char(0x0000) is None  # NUL
        assert _keysym_to_char(0x001F) is None  # control char

    def test_non_ascii_non_unicode_extension_returns_none(self) -> None:
        # 0x100 is in a keysym range not mapped here
        assert _keysym_to_char(0x0100) is None

    def test_space_is_printable(self) -> None:
        assert _keysym_to_char(0x0020) == " "

    def test_tilde_is_printable(self) -> None:
        assert _keysym_to_char(0x007E) == "~"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_adapter() -> tuple[CdpInputAdapter, MagicMock]:
    session = MagicMock()
    session.send = AsyncMock(return_value=None)
    adapter = CdpInputAdapter(session=session)
    return adapter, session


# ---------------------------------------------------------------------------
# pointer_motion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pointer_motion_fires_mouse_moved() -> None:
    adapter, session = _make_adapter()
    adapter.pointer_motion(100.5, 200.5)
    await asyncio.sleep(0)

    session.send.assert_awaited_once_with(
        "Input.dispatchMouseEvent",
        {"type": "mouseMoved", "x": 100.5, "y": 200.5},
    )


# ---------------------------------------------------------------------------
# pointer_button
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pointer_button_left_press() -> None:
    adapter, session = _make_adapter()
    adapter.pointer_button(272, True)  # BTN_LEFT
    await asyncio.sleep(0)

    args = session.send.await_args_list[0]
    assert args[0][0] == "Input.dispatchMouseEvent"
    params = args[0][1]
    assert params["type"] == "mousePressed"
    assert params["button"] == "left"
    assert params["clickCount"] == 1


@pytest.mark.asyncio
async def test_pointer_button_right_release() -> None:
    adapter, session = _make_adapter()
    adapter.pointer_button(273, False)  # BTN_RIGHT release
    await asyncio.sleep(0)

    params = session.send.await_args_list[0][0][1]
    assert params["type"] == "mouseReleased"
    assert params["button"] == "right"
    assert params["clickCount"] == 0


@pytest.mark.asyncio
async def test_pointer_button_middle_press() -> None:
    adapter, session = _make_adapter()
    adapter.pointer_button(274, True)  # BTN_MIDDLE
    await asyncio.sleep(0)

    params = session.send.await_args_list[0][0][1]
    assert params["button"] == "middle"


# ---------------------------------------------------------------------------
# pointer_axis_discrete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pointer_axis_vertical_down() -> None:
    adapter, session = _make_adapter()
    adapter.pointer_axis_discrete(0, 3)  # axis=0 vertical, steps=3
    await asyncio.sleep(0)

    params = session.send.await_args_list[0][0][1]
    assert params["type"] == "mouseWheel"
    assert params["deltaY"] == 360.0
    assert params["deltaX"] == 0.0


@pytest.mark.asyncio
async def test_pointer_axis_horizontal() -> None:
    adapter, session = _make_adapter()
    adapter.pointer_axis_discrete(1, -2)  # axis=1 horizontal, steps=-2
    await asyncio.sleep(0)

    params = session.send.await_args_list[0][0][1]
    assert params["type"] == "mouseWheel"
    assert params["deltaX"] == -240.0
    assert params["deltaY"] == 0.0


# ---------------------------------------------------------------------------
# keyboard_keysym
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keysym_press_sends_char_event() -> None:
    adapter, session = _make_adapter()
    adapter.keyboard_keysym(ord("X"), True)
    await asyncio.sleep(0)

    session.send.assert_awaited_once_with(
        "Input.dispatchKeyEvent", {"type": "char", "text": "X"}
    )


@pytest.mark.asyncio
async def test_keysym_release_is_noop() -> None:
    adapter, session = _make_adapter()
    adapter.keyboard_keysym(ord("X"), False)
    await asyncio.sleep(0)

    session.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_keysym_non_printable_skipped() -> None:
    adapter, session = _make_adapter()
    adapter.keyboard_keysym(0xFF1B, True)  # XK_Escape — not printable
    await asyncio.sleep(0)

    session.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_keysym_unicode_extension_sends_char() -> None:
    adapter, session = _make_adapter()
    keysym = 0x01000000 | ord("€")
    adapter.keyboard_keysym(keysym, True)
    await asyncio.sleep(0)

    session.send.assert_awaited_once_with(
        "Input.dispatchKeyEvent", {"type": "char", "text": "€"}
    )


# ---------------------------------------------------------------------------
# keyboard_keycode — no-op + warning
# ---------------------------------------------------------------------------


def test_keycode_does_not_call_session(caplog: pytest.LogCaptureFixture) -> None:
    adapter, session = _make_adapter()
    import logging

    with caplog.at_level(logging.WARNING, logger="hermes.browser.infrastructure.cdp_input_adapter"):
        adapter.keyboard_keycode(30, True)  # evdev KEY_A

    # No fire-and-forget scheduled (no running loop in test)
    session.send.assert_not_called()
    assert "keycode_unsupported" in caplog.text


# ---------------------------------------------------------------------------
# async_mouse_click
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_mouse_click_sends_press_then_release() -> None:
    adapter, session = _make_adapter()
    await adapter.async_mouse_click(50.0, 75.0, button="right")

    assert session.send.await_count == 2
    press = session.send.await_args_list[0][0]
    release = session.send.await_args_list[1][0]
    assert press[0] == "Input.dispatchMouseEvent"
    assert press[1]["type"] == "mousePressed"
    assert press[1]["button"] == "right"
    assert press[1]["x"] == 50.0
    assert press[1]["y"] == 75.0
    assert release[1]["type"] == "mouseReleased"


# ---------------------------------------------------------------------------
# async_type_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_type_text_sends_one_char_event_per_char() -> None:
    adapter, session = _make_adapter()
    await adapter.async_type_text("AB")

    assert session.send.await_count == 2
    for i, ch in enumerate("AB"):
        args = session.send.await_args_list[i][0]
        assert args[0] == "Input.dispatchKeyEvent"
        assert args[1] == {"type": "char", "text": ch}


@pytest.mark.asyncio
async def test_async_type_empty_string_sends_nothing() -> None:
    adapter, session = _make_adapter()
    await adapter.async_type_text("")
    session.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# stop() — no-op
# ---------------------------------------------------------------------------


def test_stop_does_not_call_session() -> None:
    adapter, session = _make_adapter()
    adapter.stop()
    session.detach = MagicMock()
    session.detach.assert_not_called()
    session.send.assert_not_called()


# ---------------------------------------------------------------------------
# SeatInputEffectorPort structural compliance
# ---------------------------------------------------------------------------


def test_conforms_to_seat_input_effector_port() -> None:
    adapter, _ = _make_adapter()
    assert isinstance(adapter, SeatInputEffectorPort), (
        "CdpInputAdapter must satisfy SeatInputEffectorPort protocol"
    )
