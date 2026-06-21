#!/usr/bin/env python3
"""Agentic Panel — overlay GTK4 + gtk4-layer-shell (research §6).

Spec 003 Phase 3 T030 — FR-014 (panel siempre visible en
personal-desktop). Es el "eye candy" del SO: barra superior con
estado del agente + bandeja de consents + indicador de actividad.

Esta es la app GTK4 que se lanza desde `hermes-runtime.service` o
desde `gnome-session` en personal-desktop. NO contiene lógica de
agente — solo presenta el estado del runtime vía D-Bus
`org.hermes.Runtime1`.

NOTA: importa gi solo si está disponible (no en CI base).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from typing import TYPE_CHECKING, Any

logger = logging.getLogger("agentic-panel")

if TYPE_CHECKING:
    from gi.repository import Gtk

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gio, GLib, Gtk, Gtk4LayerShell  # type: ignore

    _GTK_AVAILABLE = True
except (ImportError, ValueError) as exc:  # pragma: no cover
    logger.warning("GTK4 + layer-shell no disponible: %s", exc)
    _GTK_AVAILABLE = False


_PANEL_HEIGHT_PX = 36
_DBUS_BUS_NAME = "org.hermes.Runtime1"
_DBUS_PATH = "/org/hermes/Runtime1"
_DBUS_IFACE = "org.hermes.Runtime1"


class PanelModel:
    """Estado del panel desacoplado de la UI — testeable.

    El refresh real lee del DBus runtime vía adapter Python; aquí
    aceptamos snapshots ya construidos por el adapter.
    """

    def __init__(self) -> None:
        self.agent_state: str = "unknown"
        self.active_task_count: int = 0
        self.sandbox_count: int = 0
        self.telemetry_enabled: bool = False
        self.consent_count: int = 0
        self.audit_head_hex: str = ""

    def label_state(self) -> str:
        """Mensaje de estado visible en el panel."""
        labels = {
            "idle": "Hermes · en espera",
            "running": f"Hermes · trabajando · {self.active_task_count} tarea(s)",
            "paused": "Hermes · pausado",
            "unknown": "Hermes · conectando…",
        }
        return labels.get(self.agent_state, f"Hermes · {self.agent_state}")

    def telemetry_label(self) -> str:
        return "Telemetría: on" if self.telemetry_enabled else "Telemetría: off"

    def update_from_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Refresca el modelo desde un dict (lo que devuelve DBus)."""
        self.agent_state = snapshot.get("state", "unknown")
        self.active_task_count = int(snapshot.get("active_task_count", 0))
        self.sandbox_count = int(snapshot.get("sandbox_count", 0))
        self.telemetry_enabled = bool(snapshot.get("telemetry_enabled", False))
        self.consent_count = int(snapshot.get("consent_count", 0))
        self.audit_head_hex = str(snapshot.get("last_audit_head_hex", ""))


def _build_panel(model: PanelModel, app: "Gtk.Application") -> "Gtk.Window":
    """Construye la ventana GTK4 anclada al borde superior."""
    win = Gtk.ApplicationWindow(application=app, title="Hermes Panel")
    Gtk4LayerShell.init_for_window(win)
    Gtk4LayerShell.set_layer(win, Gtk4LayerShell.Layer.TOP)
    Gtk4LayerShell.set_anchor(win, Gtk4LayerShell.Edge.TOP, True)
    Gtk4LayerShell.set_anchor(win, Gtk4LayerShell.Edge.LEFT, True)
    Gtk4LayerShell.set_anchor(win, Gtk4LayerShell.Edge.RIGHT, True)
    Gtk4LayerShell.set_exclusive_zone(win, _PANEL_HEIGHT_PX)
    Gtk4LayerShell.auto_exclusive_zone_enable(win)

    win.set_default_size(-1, _PANEL_HEIGHT_PX)

    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_margin_start(12)
    box.set_margin_end(12)
    box.set_margin_top(4)
    box.set_margin_bottom(4)

    # Etiqueta de estado.
    state_label = Gtk.Label(label=model.label_state())
    state_label.set_xalign(0)
    box.append(state_label)

    spacer = Gtk.Box()
    spacer.set_hexpand(True)
    box.append(spacer)

    # Indicador de telemetría.
    telemetry_label = Gtk.Label(label=model.telemetry_label())
    box.append(telemetry_label)

    # Indicador de consents activos.
    consents_label = Gtk.Label(label=f"🔐 {model.consent_count}")
    box.append(consents_label)

    win.set_child(box)
    return win


def _refresh_panel(
    bus: "Gio.DBusConnection", model: PanelModel
) -> dict[str, Any] | None:
    """Lee el snapshot del runtime via DBus."""
    try:
        variant = bus.call_sync(
            _DBUS_BUS_NAME,
            _DBUS_PATH,
            _DBUS_IFACE,
            "GetStatus",
            None,
            GLib.VariantType("(a{sv})"),
            Gio.DBusCallFlags.NONE,
            500,  # ms
            None,
        )
        snapshot = dict(variant.unpack()[0])
        model.update_from_snapshot(snapshot)
        return snapshot
    except GLib.Error as exc:
        logger.warning("DBus GetStatus failed: %s", exc.message)
        return None


def main(argv: list[str] | None = None) -> int:
    if not _GTK_AVAILABLE:
        logger.error("GTK4 no disponible — panel no puede arrancar")
        return 1

    argv = argv or sys.argv

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    model = PanelModel()
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    app = Gtk.Application(application_id="com.hermes.AgenticPanel")

    def on_activate(application: Gtk.Application) -> None:
        win = _build_panel(model, application)
        win.present()

        def tick() -> bool:
            _refresh_panel(bus, model)
            return True

        GLib.timeout_add(1500, tick)

    app.connect("activate", on_activate)

    # SIGTERM clean shutdown (systemd).
    signal.signal(signal.SIGTERM, lambda *_: app.quit())

    return app.run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
