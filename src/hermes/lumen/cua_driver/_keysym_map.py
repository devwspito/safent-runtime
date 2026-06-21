"""X11 keysym lookup for named keys and modifier normalization.

Pure module — no I/O, no imports outside the standard library.
"""

from __future__ import annotations

# X11 keysym constants (from keysymdef.h).
# Modifiers
_KS_SHIFT_L: int = 0xFFE1
_KS_SHIFT_R: int = 0xFFE2
_KS_CTRL_L: int = 0xFFE3
_KS_CTRL_R: int = 0xFFE4
_KS_ALT_L: int = 0xFFE9
_KS_ALT_R: int = 0xFFEA
_KS_SUPER_L: int = 0xFFEB
_KS_SUPER_R: int = 0xFFEC
# Common keys
_KS_RETURN: int = 0xFF0D
_KS_ESCAPE: int = 0xFF1B
_KS_TAB: int = 0xFF09
_KS_BACKSPACE: int = 0xFF08
_KS_DELETE: int = 0xFFFF
_KS_HOME: int = 0xFF50
_KS_END: int = 0xFF57
_KS_PAGE_UP: int = 0xFF55
_KS_PAGE_DOWN: int = 0xFF56
_KS_LEFT: int = 0xFF51
_KS_UP: int = 0xFF52
_KS_RIGHT: int = 0xFF53
_KS_DOWN: int = 0xFF54
_KS_INSERT: int = 0xFF63
_KS_SPACE: int = 0x0020
_KS_F1: int = 0xFFBE
_KS_F2: int = 0xFFBF
_KS_F3: int = 0xFFC0
_KS_F4: int = 0xFFC1
_KS_F5: int = 0xFFC2
_KS_F6: int = 0xFFC3
_KS_F7: int = 0xFFC4
_KS_F8: int = 0xFFC5
_KS_F9: int = 0xFFC6
_KS_F10: int = 0xFFC7
_KS_F11: int = 0xFFC8
_KS_F12: int = 0xFFC9

# Canonical name → keysym.  Names are lower-cased before lookup.
_NAME_TO_KEYSYM: dict[str, int] = {
    "return": _KS_RETURN,
    "enter": _KS_RETURN,
    "escape": _KS_ESCAPE,
    "esc": _KS_ESCAPE,
    "tab": _KS_TAB,
    "backspace": _KS_BACKSPACE,
    "delete": _KS_DELETE,
    "del": _KS_DELETE,
    "home": _KS_HOME,
    "end": _KS_END,
    "pageup": _KS_PAGE_UP,
    "page_up": _KS_PAGE_UP,
    "pagedown": _KS_PAGE_DOWN,
    "page_down": _KS_PAGE_DOWN,
    "left": _KS_LEFT,
    "up": _KS_UP,
    "right": _KS_RIGHT,
    "down": _KS_DOWN,
    "insert": _KS_INSERT,
    "space": _KS_SPACE,
    "f1": _KS_F1,
    "f2": _KS_F2,
    "f3": _KS_F3,
    "f4": _KS_F4,
    "f5": _KS_F5,
    "f6": _KS_F6,
    "f7": _KS_F7,
    "f8": _KS_F8,
    "f9": _KS_F9,
    "f10": _KS_F10,
    "f11": _KS_F11,
    "f12": _KS_F12,
    # Modifier names — hermes-agent already normalises control→ctrl, alt→option.
    "shift": _KS_SHIFT_L,
    "shift_l": _KS_SHIFT_L,
    "shift_r": _KS_SHIFT_R,
    "ctrl": _KS_CTRL_L,
    "ctrl_l": _KS_CTRL_L,
    "ctrl_r": _KS_CTRL_R,
    "control": _KS_CTRL_L,
    "control_l": _KS_CTRL_L,
    "alt": _KS_ALT_L,
    "alt_l": _KS_ALT_L,
    "alt_r": _KS_ALT_R,
    "option": _KS_ALT_L,       # macOS alias normalised by hermes-agent
    "super": _KS_SUPER_L,
    "super_l": _KS_SUPER_L,
    "super_r": _KS_SUPER_R,
    "cmd": _KS_SUPER_L,        # macOS alias
    "command": _KS_SUPER_L,
    "meta": _KS_SUPER_L,
    "win": _KS_SUPER_L,
}

# Modifier names that must be pressed before the primary key.
MODIFIER_NAMES: frozenset[str] = frozenset({
    "shift", "shift_l", "shift_r",
    "ctrl", "ctrl_l", "ctrl_r", "control", "control_l",
    "alt", "alt_l", "alt_r", "option",
    "super", "super_l", "super_r", "cmd", "command", "meta", "win",
})


def named_key_to_keysym(name: str) -> int:
    """Return the X11 keysym for a named key string.

    Single-character strings are mapped via _char_to_keysym.
    Multi-character strings are looked up in _NAME_TO_KEYSYM.

    Raises:
        ValueError: if the name is unknown.
    """
    if len(name) == 1:
        return _char_to_keysym(name)
    canonical = name.lower()
    keysym = _NAME_TO_KEYSYM.get(canonical)
    if keysym is None:
        raise ValueError(f"Unknown key name: {name!r}")
    return keysym


def _char_to_keysym(char: str) -> int:
    """Map a Unicode character to its X11 keysym.

    Reuses the same rule as input_bridge._char_to_keysym (kept in sync):
      ASCII printable 0x20..0x7E → keysym == ord(char)
      Higher code points         → 0x01000000 | codepoint
    """
    cp = ord(char)
    if 0x20 <= cp <= 0x7E:
        return cp
    return 0x01000000 | cp


def is_modifier(name: str) -> bool:
    """True if the key name refers to a modifier key."""
    return name.lower() in MODIFIER_NAMES
