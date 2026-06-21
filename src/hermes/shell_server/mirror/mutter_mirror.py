"""MutterMirrorSession — espejo nativo: captura (ScreenCast) + control (RemoteDesktop).

A diferencia de MutterScreenCastSource (solo lectura), aquí creamos una sesión
de RemoteDesktop y le ENLAZAMOS la de ScreenCast (`remote-desktop-session-id`),
para poder además INYECTAR input (ratón/teclado) sobre el mismo stream. Es el
mismo mecanismo que usa gnome-remote-desktop, pero sin su contraseña/llavero:
la autenticación la hace nuestra capa (token) en el server.

Vive en el proceso que tiene el bus de SESIÓN de mutter (servicio de usuario en
la sesión Hermes Shell). Reutiliza el flujo validado de mutter_source.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from ..screen_capture.domain import CaptureError
from .button_codes import BTN_LEFT, BTN_MIDDLE, BTN_RIGHT  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

_RD = "org.gnome.Mutter.RemoteDesktop"
_RD_PATH = "/org/gnome/Mutter/RemoteDesktop"
_SC = "org.gnome.Mutter.ScreenCast"
_SC_PATH = "/org/gnome/Mutter/ScreenCast"
_DC = "org.gnome.Mutter.DisplayConfig"
_DC_PATH = "/org/gnome/Mutter/DisplayConfig"
_PROPS = "org.freedesktop.DBus.Properties"

_CURSOR_EMBEDDED = 1  # pintamos el cursor en el stream


class MutterMirrorSession:
    """Sesión RemoteDesktop+ScreenCast: entrega node PipeWire + inyecta input."""

    def __init__(self, bus: Gio.DBusConnection | None = None) -> None:
        self._bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._rd_path: str | None = None
        self._rd_session: Gio.DBusProxy | None = None
        self._sc_path: str | None = None
        self._stream_path: str | None = None
        self._sub_id: int | None = None

    # ------------------------------------------------------------------
    def _primary_connector(self) -> str:
        dc = Gio.DBusProxy.new_sync(
            self._bus, Gio.DBusProxyFlags.NONE, None, _DC, _DC_PATH, _DC, None
        )
        state = dc.call_sync(
            "GetCurrentState", None, Gio.DBusCallFlags.NONE, -1, None
        )
        monitors = state.unpack()[1]
        if not monitors:
            raise CaptureError("mutter DisplayConfig: no monitors")
        return monitors[0][0][0]

    def start(self, *, timeout_s: int = 8) -> int:
        """Levanta RemoteDesktop + ScreenCast enlazados. Devuelve PipeWire node_id."""
        rd = Gio.DBusProxy.new_sync(
            self._bus, Gio.DBusProxyFlags.NONE, None, _RD, _RD_PATH, _RD, None
        )
        self._rd_path = rd.call_sync(
            "CreateSession", None, Gio.DBusCallFlags.NONE, -1, None
        ).unpack()[0]
        self._rd_session = Gio.DBusProxy.new_sync(
            self._bus, Gio.DBusProxyFlags.NONE, None,
            _RD, self._rd_path, _RD + ".Session", None,
        )
        # SessionId (propiedad) que enlaza el ScreenCast a este RemoteDesktop.
        props = Gio.DBusProxy.new_sync(
            self._bus, Gio.DBusProxyFlags.NONE, None,
            _RD, self._rd_path, _PROPS, None,
        )
        rd_session_id = props.call_sync(
            "Get",
            GLib.Variant("(ss)", (_RD + ".Session", "SessionId")),
            Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()[0]
        logger.info("RemoteDesktop session=%s id=%s", self._rd_path, rd_session_id)

        sc = Gio.DBusProxy.new_sync(
            self._bus, Gio.DBusProxyFlags.NONE, None, _SC, _SC_PATH, _SC, None
        )
        self._sc_path = sc.call_sync(
            "CreateSession",
            GLib.Variant("(a{sv})", (
                {"remote-desktop-session-id": GLib.Variant("s", rd_session_id)},
            )),
            Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()[0]
        sc_session = Gio.DBusProxy.new_sync(
            self._bus, Gio.DBusProxyFlags.NONE, None,
            _SC, self._sc_path, _SC + ".Session", None,
        )
        connector = self._primary_connector()
        self._stream_path = sc_session.call_sync(
            "RecordMonitor",
            GLib.Variant("(sa{sv})", (
                connector, {"cursor-mode": GLib.Variant("u", _CURSOR_EMBEDDED)},
            )),
            Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()[0]

        node_holder: dict[str, int] = {}
        loop = GLib.MainLoop()

        def _on_signal(_c, _s, _p, _i, _signal, params):
            node_holder["id"] = params.unpack()[0]
            loop.quit()

        self._sub_id = self._bus.signal_subscribe(
            None, _SC + ".Stream", "PipeWireStreamAdded", self._stream_path,
            None, Gio.DBusSignalFlags.NONE, _on_signal,
        )
        # El ScreenCast enlazado se arranca DESDE la sesión RemoteDesktop
        # (rd.Start()), NO con sc.Start() directo — eso da el error
        # "Must be started from remote desktop session". rd.Start() levanta el
        # SC enlazado y dispara PipeWireStreamAdded con el node.
        self._rd_session.call_sync("Start", None, Gio.DBusCallFlags.NONE, -1, None)
        GLib.timeout_add_seconds(timeout_s, lambda: (loop.quit(), False)[1])
        loop.run()

        if "id" not in node_holder:
            self.stop()
            raise CaptureError("mutter no entregó PipeWire node (timeout)")
        logger.info("mirror stream=%s node=%s", self._stream_path, node_holder["id"])
        return node_holder["id"]

    # ---- inyección de input (síncrona; seguro desde cualquier hilo) ----
    def pointer_motion(self, x: float, y: float) -> None:
        self._rd_session.call_sync(
            "NotifyPointerMotionAbsolute",
            GLib.Variant("(sdd)", (self._stream_path, x, y)),
            Gio.DBusCallFlags.NONE, -1, None,
        )

    def pointer_button(self, button: int, pressed: bool) -> None:
        self._rd_session.call_sync(
            "NotifyPointerButton",
            GLib.Variant("(ib)", (button, pressed)),
            Gio.DBusCallFlags.NONE, -1, None,
        )

    def pointer_axis_discrete(self, axis: int, steps: int) -> None:
        # axis: 0=vertical, 1=horizontal. steps: +/- 1.
        self._rd_session.call_sync(
            "NotifyPointerAxisDiscrete",
            GLib.Variant("(ui)", (axis, steps)),
            Gio.DBusCallFlags.NONE, -1, None,
        )

    def keyboard_keysym(self, keysym: int, pressed: bool) -> None:
        self._rd_session.call_sync(
            "NotifyKeyboardKeysym",
            GLib.Variant("(ub)", (keysym, pressed)),
            Gio.DBusCallFlags.NONE, -1, None,
        )

    def keyboard_keycode(self, keycode: int, pressed: bool) -> None:
        """Inyecta una tecla por KEYCODE evdev (no keysym).

        Es el método que usan RDP/VNC/gnome-remote-desktop: la VM aplica su
        propio keymap a la tecla física, y los modificadores (Shift/Caps/...)
        son teclas físicas reales — sin la síntesis keysym->nivel que latchea
        el shift. El navegador manda el keycode evdev derivado de `event.code`.
        """
        self._rd_session.call_sync(
            "NotifyKeyboardKeycode",
            GLib.Variant("(ub)", (keycode, pressed)),
            Gio.DBusCallFlags.NONE, -1, None,
        )

    def stop(self) -> None:
        if self._sub_id is not None:
            self._bus.signal_unsubscribe(self._sub_id)
            self._sub_id = None
        for path, iface in ((self._sc_path, _SC), (self._rd_path, _RD)):
            if not path:
                continue
            try:
                Gio.DBusProxy.new_sync(
                    self._bus, Gio.DBusProxyFlags.NONE, None,
                    iface, path, iface + ".Session", None,
                ).call_sync("Stop", None, Gio.DBusCallFlags.NONE, -1, None)
            except GLib.GError:
                logger.debug("sesión ya cerrada", exc_info=True)
        self._rd_path = self._sc_path = self._stream_path = None
        self._rd_session = None
