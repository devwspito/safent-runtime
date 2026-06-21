"""SeatInputEffectorPort — abstract contract for seat input injection.

Both MutterMirrorSession (desktop via Mutter RemoteDesktop D-Bus) and
SeatInputAdapter (pocket via QWaylandSeat) implement this Protocol.

All security enforcement (token auth, rate limiting, chord denylist,
InputOwnershipLedger contention guard) lives in SessionInputBridge and
does NOT change regardless of which implementation is injected.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SeatInputEffectorPort(Protocol):
    """Inject pointer and keyboard events into the Wayland seat."""

    def pointer_motion(self, x: float, y: float) -> None:
        """Move the pointer to absolute compositor coordinates (x, y)."""
        ...

    def pointer_button(self, button: int, pressed: bool) -> None:
        """Press or release a pointer button (evdev BTN_* code)."""
        ...

    def pointer_axis_discrete(self, axis: int, steps: int) -> None:
        """Scroll by discrete steps. axis: 0=vertical, 1=horizontal."""
        ...

    def keyboard_keysym(self, keysym: int, pressed: bool) -> None:
        """Inject a key event by X11 keysym."""
        ...

    def keyboard_keycode(self, keycode: int, pressed: bool) -> None:
        """Inject a key event by evdev keycode (layout-independent)."""
        ...

    def stop(self) -> None:
        """Release resources held by this effector."""
        ...
