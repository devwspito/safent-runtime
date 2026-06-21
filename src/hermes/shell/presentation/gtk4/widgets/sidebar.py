"""HermesSidebar — navegación principal izquierda.

Layout (top → bottom):
  1. Brand "Hermes"
  2. AgentSelector  ← selector de agente activo + popover de gestión
  3. Botón "Nueva conversación"
  4. Sección "Asistente" → Inicio
  5. Sección "Recientes" → lista de conversaciones recientes
  6. Sección "Herramientas" → Skills, Integraciones, Tareas, Auditoría, Acceso remoto
  7. Spacer
  8. Ajustes

El "workspace" real es el panel derecho (pantalla en vivo del agente),
no un item de menú.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk  # noqa: E402

from hermes.shell.domain.shell_session import ShellView
from hermes.shell.infrastructure.shell_backend_client import ShellBackendClient
from hermes.shell.presentation.gtk4.widgets.agent_selector import AgentSelector
from hermes.shell.presentation.gtk4.widgets.recent_conversations import (
    RecentConversations,
)


# (view_id, label, icon-name).
_PRIMARY = (ShellView.HOME.value, "Inicio", "user-available-symbolic")
_TOOLS: list[tuple[str, str, str]] = [
    (ShellView.SKILLS.value, "Skills", "preferences-system-symbolic"),
    (ShellView.INTEGRATIONS.value, "Integraciones", "network-transmit-receive-symbolic"),
    (ShellView.TASKS.value, "Tareas", "task-due-symbolic"),
    (ShellView.AUDIT.value, "Auditoría", "document-properties-symbolic"),
    (ShellView.REMOTE.value, "Acceso remoto", "network-wireless-symbolic"),
]
_SETTINGS = (ShellView.SETTINGS.value, "Ajustes", "emblem-system-symbolic")


class HermesSidebar(Gtk.Box):
    """Sidebar con brand + selector agente + nueva conversación + navegación."""

    __gsignals__ = {
        "view-selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "new-conversation": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "conversation-selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # Delega señales del AgentSelector hacia arriba (para window.py).
        "create-agent": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "edit-agent": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "delete-agent": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "agent-selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, *, active: ShellView, client: ShellBackendClient) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("hermes-sidebar")

        # Brand.
        brand = Gtk.Label(label="Hermes")
        brand.set_xalign(0)
        brand.add_css_class("hermes-sidebar-brand")
        self.append(brand)

        # Agent selector.
        self._agent_selector = AgentSelector()
        self._agent_selector.set_margin_bottom(8)
        self._agent_selector.connect("create-agent", lambda _w: self.emit("create-agent"))
        self._agent_selector.connect("edit-agent", lambda _w, aid: self.emit("edit-agent", aid))
        self._agent_selector.connect("delete-agent", lambda _w, aid: self.emit("delete-agent", aid))
        self._agent_selector.connect("agent-selected", lambda _w, aid: self.emit("agent-selected", aid))
        self.append(self._agent_selector)

        # + Nueva conversación.
        new_btn = Gtk.Button()
        new_btn.add_css_class("hermes-sidebar-new")
        new_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        new_box.append(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        new_lbl = Gtk.Label(label="Nueva conversación")
        new_lbl.set_xalign(0)
        new_box.append(new_lbl)
        new_btn.set_child(new_box)
        new_btn.connect("clicked", lambda _b: self.emit("new-conversation"))
        self.append(new_btn)

        # Asistente.
        self._add_section_title("Asistente")
        self._add_item(*_PRIMARY, active=active)

        # Recientes.
        self._recents = RecentConversations(client=client)
        self._recents.connect(
            "conversation-selected",
            lambda _w, cid: self.emit("conversation-selected", cid),
        )
        self.append(self._recents)

        # Herramientas.
        self._add_section_title("Herramientas")
        for view_id, label, icon in _TOOLS:
            self._add_item(view_id, label, icon, active=active)

        # Spacer.
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        self.append(spacer)

        # Sistema.
        self._add_item(*_SETTINGS, active=active)

    # ------------------------------------------------------------------
    # Public: proxy to agent selector
    # ------------------------------------------------------------------

    @property
    def agent_selector(self) -> AgentSelector:
        return self._agent_selector

    @property
    def recent_conversations(self) -> RecentConversations:
        return self._recents

    # ------------------------------------------------------------------
    # Private builders
    # ------------------------------------------------------------------

    def _add_section_title(self, text: str) -> None:
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0)
        lbl.add_css_class("hermes-sidebar-section-title")
        self.append(lbl)

    def _add_item(
        self, view_id: str, label: str, icon_name: str, *, active: ShellView
    ) -> None:
        btn = Gtk.Button()
        btn.add_css_class("hermes-sidebar-item")
        btn.set_has_frame(False)
        if view_id == active.value:
            btn.add_css_class("active")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.append(Gtk.Image.new_from_icon_name(icon_name))
        label_w = Gtk.Label(label=label)
        label_w.set_xalign(0)
        box.append(label_w)
        btn.set_child(box)

        btn.connect("clicked", self._on_clicked, view_id)
        self.append(btn)

    def _on_clicked(self, _btn, view_id: str) -> None:
        self.emit("view-selected", view_id)
