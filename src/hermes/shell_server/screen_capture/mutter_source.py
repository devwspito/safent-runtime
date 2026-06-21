"""MutterScreenCastSource — abre un stream PipeWire vía mutter D-Bus.

Encapsula el flujo validado en el spike:
  DisplayConfig.GetCurrentState  → connector del monitor
  ScreenCast.CreateSession       → session path
  Session.RecordMonitor/Window   → stream path
  Stream.PipeWireStreamAdded     → node_id (lo que consume pipewiresrc)
  Session.Start

Vive en el proceso que tiene el bus de SESIÓN de mutter (el shell GTK4 en
producción, o un proceso de la misma sesión de usuario). NO en el shell-server
system daemon, que no está en la sesión gráfica.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .domain import CaptureError, CaptureTarget, CaptureTargetKind

logger = logging.getLogger(__name__)

_SC = "org.gnome.Mutter.ScreenCast"
_SC_PATH = "/org/gnome/Mutter/ScreenCast"
_DC = "org.gnome.Mutter.DisplayConfig"
_DC_PATH = "/org/gnome/Mutter/DisplayConfig"

# cursor-mode: 0=hidden, 1=embedded (lo pintamos en el stream), 2=metadata.
_CURSOR_EMBEDDED = 1


class MutterScreenCastSource:
    """Crea/levanta una sesión de ScreenCast y entrega el PipeWire node_id."""

    def __init__(self, bus: Gio.DBusConnection | None = None) -> None:
        self._bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._session_path: str | None = None
        self._stream_path: str | None = None
        self._sub_id: int | None = None

    # ------------------------------------------------------------------
    def primary_connector(self) -> str:
        """Connector del primer monitor (p.ej. 'Virtual-1')."""
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

    def start(self, target: CaptureTarget, *, timeout_s: int = 8) -> int:
        """Levanta la sesión y devuelve el PipeWire node_id.

        Bloquea hasta recibir PipeWireStreamAdded o agotar timeout.
        """
        sc = Gio.DBusProxy.new_sync(
            self._bus, Gio.DBusProxyFlags.NONE, None, _SC, _SC_PATH, _SC, None
        )
        self._session_path = sc.call_sync(
            "CreateSession",
            GLib.Variant("(a{sv})", ({},)),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        ).unpack()[0]
        logger.info("ScreenCast session=%s", self._session_path)

        sess = Gio.DBusProxy.new_sync(
            self._bus,
            Gio.DBusProxyFlags.NONE,
            None,
            _SC,
            self._session_path,
            _SC + ".Session",
            None,
        )

        props = {"cursor-mode": GLib.Variant("u", _CURSOR_EMBEDDED)}
        if target.kind == CaptureTargetKind.MONITOR:
            connector = target.monitor_connector or self.primary_connector()
            self._stream_path = sess.call_sync(
                "RecordMonitor",
                GLib.Variant("(sa{sv})", (connector, props)),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            ).unpack()[0]
        elif target.kind == CaptureTargetKind.WINDOW:
            if target.window_id is None:
                raise CaptureError("WINDOW target requires window_id")
            wprops = dict(props)
            wprops["window-id"] = GLib.Variant("t", target.window_id)
            self._stream_path = sess.call_sync(
                "RecordWindow",
                GLib.Variant("(a{sv})", (wprops,)),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            ).unpack()[0]
        else:  # pragma: no cover - enum exhaustivo
            raise CaptureError(f"target kind no soportado: {target.kind}")

        logger.info("ScreenCast stream=%s", self._stream_path)

        node_holder: dict[str, int] = {}
        loop = GLib.MainLoop()

        def _on_signal(_c, _s, _p, _i, signal, params):
            if signal == "PipeWireStreamAdded":
                node_holder["id"] = params.unpack()[0]
                loop.quit()

        self._sub_id = self._bus.signal_subscribe(
            None,
            _SC + ".Stream",
            "PipeWireStreamAdded",
            self._stream_path,
            None,
            Gio.DBusSignalFlags.NONE,
            _on_signal,
        )

        sess.call_sync("Start", None, Gio.DBusCallFlags.NONE, -1, None)
        GLib.timeout_add_seconds(timeout_s, lambda: (loop.quit(), False)[1])
        loop.run()

        if "id" not in node_holder:
            self.stop()
            raise CaptureError("mutter no entregó PipeWire node (timeout)")
        return node_holder["id"]

    def stop(self) -> None:
        if self._sub_id is not None:
            self._bus.signal_unsubscribe(self._sub_id)
            self._sub_id = None
        if self._session_path:
            try:
                sess = Gio.DBusProxy.new_sync(
                    self._bus,
                    Gio.DBusProxyFlags.NONE,
                    None,
                    _SC,
                    self._session_path,
                    _SC + ".Session",
                    None,
                )
                sess.call_sync("Stop", None, Gio.DBusCallFlags.NONE, -1, None)
            except GLib.GError:  # pragma: no cover - best effort
                logger.debug("ScreenCast session ya cerrada", exc_info=True)
            self._session_path = None
            self._stream_path = None
