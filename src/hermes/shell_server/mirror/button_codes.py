"""Evdev button codes shared by mirror implementations.

Extracted from mutter_mirror.py so both the desktop adapter
(MutterMirrorSession) and the pocket adapter (SeatInputAdapter) can
import them without a circular dependency.
"""

from __future__ import annotations

# Standard Linux evdev button codes (input-event-codes.h).
BTN_LEFT: int = 0x110
BTN_RIGHT: int = 0x111
BTN_MIDDLE: int = 0x112
