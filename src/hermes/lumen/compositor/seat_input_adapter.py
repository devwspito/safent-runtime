"""SeatInputAdapter — SeatInputEffectorPort backed by QWaylandSeat.

Implements the same interface as MutterMirrorSession so SessionInputBridge
can inject pointer/keyboard events into the pocket compositor without any
changes to the security layer (token, rate-limit, chord denylist, ownership
contention guard all stay in the bridge, unchanged).

Thread safety: all Qt Wayland calls must run on the GUI thread.  Each method
marshals its payload to the GUI thread via QMetaObject.invokeMethod with
Qt.QueuedConnection when called from the bridge's asyncio thread pool.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWaylandCompositor import QWaylandSeat  # noqa: F401

logger = logging.getLogger(__name__)


class SeatInputAdapter:
    """Routes bridge input calls to QWaylandSeat on the GUI thread.

    Args:
        seat: The QWaylandSeat instance owned by the compositor.
              Pass None to construct in test/offline mode (calls are no-ops).
    """

    def __init__(self, seat: "QWaylandSeat | None" = None) -> None:
        self._seat = seat

    def pointer_motion(self, x: float, y: float) -> None:
        """Move pointer to absolute surface coordinates (x, y)."""
        if self._seat is None:
            return
        self._invoke(self._seat.sendMouseMoveEvent, None, x, y)

    def pointer_button(self, button: int, pressed: bool) -> None:
        """Press/release a pointer button (evdev BTN_* code)."""
        if self._seat is None:
            return
        # TODO(H0-HARDWARE): verify QWaylandSeat.sendMousePressEvent /
        # sendMouseReleaseEvent evdev→Qt button mapping on RK3588 display stack.
        from PySide6.QtCore import Qt
        qt_button = self._evdev_btn_to_qt(button)
        if pressed:
            self._invoke(self._seat.sendMousePressEvent, None, qt_button)
        else:
            self._invoke(self._seat.sendMouseReleaseEvent, None, qt_button)

    def pointer_axis_discrete(self, axis: int, steps: int) -> None:
        """Scroll by discrete steps. axis: 0=vertical, 1=horizontal."""
        if self._seat is None:
            return
        # TODO(H0-HARDWARE): test wheel event delivery on RK3588 with libinput.
        from PySide6.QtCore import QPoint
        delta = QPoint(0, steps * 120) if axis == 0 else QPoint(steps * 120, 0)
        self._invoke(self._seat.sendMouseWheelEvent, None, delta)

    def keyboard_keysym(self, keysym: int, pressed: bool) -> None:
        """Inject a key event by X11 keysym.

        Qt Wayland accepts xkb keysyms via sendKeyEvent.
        """
        if self._seat is None:
            return
        from PySide6.QtCore import Qt
        # Qt key() maps directly to X11 keysym for the Latin-1 range and common
        # function keys; higher code points use the 0x01000000 | cp convention
        # which matches X11's Unicode range — exactly what _char_to_keysym
        # in input_bridge produces.
        qt_key = Qt.Key(keysym)
        event_type = (
            "QEvent::KeyPress" if pressed else "QEvent::KeyRelease"
        )
        # TODO(H0-HARDWARE): validate that sendKeyEvent honours xkb keysyms
        # (not scan codes) with the Rockchip Mali display pipeline.
        self._invoke(self._seat.sendKeyEvent, None, qt_key, pressed)

    def keyboard_keycode(self, keycode: int, pressed: bool) -> None:
        """Inject a key event by evdev keycode (layout-independent).

        evdev keycodes need to be translated to xkb scancodes (+8 offset)
        before they reach the Wayland seat.
        """
        if self._seat is None:
            return
        # evdev → xkb scancode: xkb scancode = evdev keycode + 8 (per XKB spec).
        xkb_scancode = keycode + 8
        # TODO(H0-HARDWARE): verify xkb scancode delivery matches hardware keymap
        # on RK3588 / libinput.  On desktop this matches gnome-remote-desktop
        # NotifyKeyboardKeycode which also applies the +8 offset internally.
        from PySide6.QtWaylandCompositor import QWaylandSeat
        self._invoke(self._seat.sendFullKeyEvent, None, xkb_scancode, pressed)

    def stop(self) -> None:
        """Release the seat reference.  Compositor owns the seat lifetime."""
        self._seat = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _invoke(self, method, *args) -> None:
        """Marshal a seat call to the GUI thread (QueuedConnection)."""
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(
            self._seat,
            method.__name__,
            Qt.ConnectionType.QueuedConnection,
            *args,
        )

    @staticmethod
    def _evdev_btn_to_qt(evdev_button: int) -> "Qt.MouseButton":
        """Map evdev BTN_LEFT/RIGHT/MIDDLE to Qt.MouseButton."""
        from PySide6.QtCore import Qt
        from hermes.shell_server.mirror.button_codes import BTN_LEFT, BTN_MIDDLE, BTN_RIGHT

        _MAP = {
            BTN_LEFT:   Qt.MouseButton.LeftButton,
            BTN_RIGHT:  Qt.MouseButton.RightButton,
            BTN_MIDDLE: Qt.MouseButton.MiddleButton,
        }
        return _MAP.get(evdev_button, Qt.MouseButton.LeftButton)
