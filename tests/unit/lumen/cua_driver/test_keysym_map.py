"""Unit tests for _keysym_map — pure keysym lookup, no I/O."""

from __future__ import annotations

import pytest

from hermes.lumen.cua_driver._keysym_map import (
    named_key_to_keysym,
    is_modifier,
    _char_to_keysym,
)

pytestmark = pytest.mark.unit


class TestCharToKeysym:
    def test_ascii_printable_returns_ordinal(self) -> None:
        assert _char_to_keysym("A") == ord("A")
        assert _char_to_keysym(" ") == 0x20
        assert _char_to_keysym("~") == 0x7E

    def test_unicode_above_7e_uses_unicode_range(self) -> None:
        # Euro sign U+20AC → 0x01000000 | 0x20AC
        assert _char_to_keysym("€") == 0x010020AC

    def test_tab_is_not_ascii_printable(self) -> None:
        # Tab is 0x09, below 0x20 — should use Unicode range
        assert _char_to_keysym("\t") == 0x01000009


class TestNamedKeyToKeysym:
    def test_return_key(self) -> None:
        assert named_key_to_keysym("return") == 0xFF0D

    def test_enter_alias(self) -> None:
        assert named_key_to_keysym("enter") == 0xFF0D

    def test_escape_key(self) -> None:
        assert named_key_to_keysym("escape") == 0xFF1B

    def test_esc_alias(self) -> None:
        assert named_key_to_keysym("esc") == 0xFF1B

    def test_tab_key(self) -> None:
        assert named_key_to_keysym("tab") == 0xFF09

    def test_backspace_key(self) -> None:
        assert named_key_to_keysym("backspace") == 0xFF08

    def test_arrow_keys(self) -> None:
        assert named_key_to_keysym("left") == 0xFF51
        assert named_key_to_keysym("up") == 0xFF52
        assert named_key_to_keysym("right") == 0xFF53
        assert named_key_to_keysym("down") == 0xFF54

    def test_function_keys(self) -> None:
        assert named_key_to_keysym("f1") == 0xFFBE
        assert named_key_to_keysym("f12") == 0xFFC9

    def test_ctrl_modifier(self) -> None:
        assert named_key_to_keysym("ctrl") == 0xFFE3

    def test_alt_modifier(self) -> None:
        assert named_key_to_keysym("alt") == 0xFFE9

    def test_option_alias_for_alt(self) -> None:
        # hermes-agent normalises option → handled by cua_driver
        assert named_key_to_keysym("option") == named_key_to_keysym("alt")

    def test_single_char_dispatched_to_char_to_keysym(self) -> None:
        assert named_key_to_keysym("a") == ord("a")

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown key name"):
            named_key_to_keysym("notakey")

    def test_case_insensitive_lookup(self) -> None:
        assert named_key_to_keysym("Return") == named_key_to_keysym("return")
        assert named_key_to_keysym("ESCAPE") == named_key_to_keysym("escape")


class TestIsModifier:
    def test_ctrl_is_modifier(self) -> None:
        assert is_modifier("ctrl") is True

    def test_alt_is_modifier(self) -> None:
        assert is_modifier("alt") is True

    def test_shift_is_modifier(self) -> None:
        assert is_modifier("shift") is True

    def test_return_is_not_modifier(self) -> None:
        assert is_modifier("return") is False

    def test_c_is_not_modifier(self) -> None:
        assert is_modifier("c") is False
