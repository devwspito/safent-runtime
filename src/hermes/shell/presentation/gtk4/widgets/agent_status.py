"""HermesAgentStatusBar — barra superior con estado del agente y conexión runtime."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from hermes.shell.domain.shell_session import RuntimeLinkState


class HermesAgentStatusBar(Gtk.Box):
    """Barra superior del center pane: dot + texto + acciones."""

    def __init__(self, *, link_state: RuntimeLinkState) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_css_class("hermes-agent-status")

        self._dot = Gtk.Box()
        self._dot.add_css_class("hermes-agent-status-dot")
        self.append(self._dot)

        self._label = Gtk.Label()
        self._label.set_xalign(0)
        self._label.add_css_class("hermes-agent-status-label")
        self.append(self._label)

        # Spacer + acciones (right side).
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.append(spacer)

        self._link_dot = Gtk.Label()
        self._link_dot.add_css_class("hermes-agent-status-label")
        self.append(self._link_dot)

        self.set_link_state(link_state)
        self.set_agent_activity("idle")

    def set_agent_activity(self, activity: str) -> None:
        for css in ("idle", "thinking", "acting", "waiting"):
            self._dot.remove_css_class(css)
        self._dot.add_css_class(activity)
        labels = {
            "idle": "Hermes — listo",
            "thinking": "Hermes — pensando…",
            "acting": "Hermes — ejecutando",
            "waiting": "Hermes — esperando tu aprobación",
        }
        self._label.set_text(labels.get(activity, f"Hermes — {activity}"))

    def set_link_state(self, link_state: RuntimeLinkState) -> None:
        labels = {
            RuntimeLinkState.CONNECTED: "● Runtime conectado",
            RuntimeLinkState.RECONNECTING: "● Conectando…",
            RuntimeLinkState.OFFLINE: "○ Runtime offline",
            RuntimeLinkState.DEGRADED: "● Degradado",
        }
        self._link_dot.set_text(labels.get(link_state, link_state.value))
