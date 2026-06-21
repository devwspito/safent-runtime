"""AgentSelector — selector de agente activo en la barra lateral.

Muestra el agente activo (punto de color + nombre) como botón. Al pulsarlo
abre un popover con la lista de agentes disponibles, acciones Editar/Eliminar
por agente, y un botón Crear agente.

Diseño: cliente fino. NO habla D-Bus directamente. Expone señales y delega
la lógica async al HermesShellWindow (via GLib.idle_add, igual que el chat).
"""

from __future__ import annotations

import logging
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, GLib, Gtk  # noqa: E402

logger = logging.getLogger(__name__)

# Paleta de colores de agente — mapeada desde el campo `color` del dict.
# Si el color no está en el mapa se usa hermes_accent como fallback.
_COLOR_MAP: dict[str, str] = {
    "indigo": "#4F46E5",
    "blue": "#2563EB",
    "green": "#16A34A",
    "amber": "#D97706",
    "red": "#DC2626",
    "purple": "#7C3AED",
    "pink": "#DB2777",
    "teal": "#0D9488",
    "gray": "#6B7280",
}

_FALLBACK_COLOR = "#4F46E5"


def _agent_color(agent: dict) -> str:
    raw = (agent.get("color") or "").lower()
    return _COLOR_MAP.get(raw, _FALLBACK_COLOR)


class AgentSelector(Gtk.Box):
    """Botón del agente activo + popover de selección/gestión."""

    __gsignals__ = {
        # Emitido cuando el usuario pide crear un agente nuevo.
        "create-agent": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # Emitido cuando el usuario pide editar un agente (agent_id).
        "edit-agent": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # Emitido cuando el usuario confirma eliminar un agente (agent_id).
        "delete-agent": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # Emitido cuando el usuario selecciona un agente diferente (agent_id).
        "agent-selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Estado interno: lista cacheada de agentes + agente activo.
        self._agents: list[dict] = []
        self._active_agent_id: str | None = None

        # Botón principal que muestra el agente activo.
        self._trigger_btn = Gtk.Button()
        self._trigger_btn.add_css_class("hermes-agent-selector-btn")
        self._trigger_btn.set_has_frame(False)
        self._trigger_btn.connect("clicked", self._on_trigger_clicked)
        self.append(self._trigger_btn)

        self._trigger_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._dot = Gtk.DrawingArea()
        self._dot.set_size_request(10, 10)
        self._dot.add_css_class("hermes-agent-dot")
        self._trigger_box.append(self._dot)

        self._name_label = Gtk.Label(label="Agente")
        self._name_label.set_xalign(0)
        self._name_label.set_hexpand(True)
        self._name_label.add_css_class("hermes-agent-selector-name")
        self._trigger_box.append(self._name_label)

        chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        chevron.add_css_class("hermes-agent-selector-chevron")
        self._trigger_box.append(chevron)
        self._trigger_btn.set_child(self._trigger_box)

        # Popover (creado bajo demanda).
        self._popover: Gtk.Popover | None = None

        # Color actual del dot (hex).
        self._current_color = _FALLBACK_COLOR
        self._dot.set_draw_func(self._draw_dot)

    # ------------------------------------------------------------------
    # Public API — llamado por window.py desde el hilo GTK via GLib.idle_add
    # ------------------------------------------------------------------

    def set_agents(self, agents: list[dict], active_agent_id: str | None) -> None:
        """Actualiza la lista y el agente activo. Debe llamarse en GTK main thread."""
        self._agents = agents
        self._active_agent_id = active_agent_id
        self._refresh_trigger()

    def _refresh_trigger(self) -> None:
        active = self._find_active()
        if active:
            self._name_label.set_text(active.get("name", "Agente"))
            self._current_color = _agent_color(active)
        else:
            self._name_label.set_text("Agente")
            self._current_color = _FALLBACK_COLOR
        self._dot.queue_draw()

    def _find_active(self) -> dict | None:
        for a in self._agents:
            if a.get("agent_id") == self._active_agent_id:
                return a
        return self._agents[0] if self._agents else None

    # ------------------------------------------------------------------
    # Dot drawing
    # ------------------------------------------------------------------

    def _draw_dot(self, area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        # Parse hex color.
        color = self._current_color.lstrip("#")
        try:
            r = int(color[0:2], 16) / 255
            g = int(color[2:4], 16) / 255
            b = int(color[4:6], 16) / 255
        except (ValueError, IndexError):
            r, g, b = 0.31, 0.27, 0.90  # indigo fallback
        cx = width / 2
        cy = height / 2
        radius = min(cx, cy)
        cr.arc(cx, cy, radius, 0, 2 * 3.14159)
        cr.set_source_rgb(r, g, b)
        cr.fill()

    # ------------------------------------------------------------------
    # Popover
    # ------------------------------------------------------------------

    def _on_trigger_clicked(self, _btn: Gtk.Button) -> None:
        if self._popover is not None:
            self._popover.popdown()
            self._popover = None

        popover = Gtk.Popover()
        popover.set_parent(self._trigger_btn)
        popover.set_has_arrow(False)
        popover.set_size_request(260, -1)
        popover.add_css_class("hermes-agent-popover")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_top(6)
        outer.set_margin_bottom(6)
        outer.set_margin_start(6)
        outer.set_margin_end(6)

        # Lista de agentes.
        if self._agents:
            list_box = Gtk.ListBox()
            list_box.set_selection_mode(Gtk.SelectionMode.NONE)
            list_box.add_css_class("hermes-agent-list")
            for agent in self._agents:
                list_box.append(self._build_agent_row(agent, popover))
            outer.append(list_box)

            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_margin_top(4)
            sep.set_margin_bottom(4)
            outer.append(sep)
        else:
            empty = Gtk.Label(label="Sin agentes")
            empty.add_css_class("dim-label")
            empty.set_margin_top(8)
            empty.set_margin_bottom(8)
            outer.append(empty)

        # Botón Crear agente.
        create_btn = Gtk.Button()
        create_btn.add_css_class("hermes-agent-create-btn")
        create_btn.set_has_frame(False)
        create_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        create_box.set_margin_top(4)
        create_box.set_margin_bottom(4)
        create_box.set_margin_start(8)
        create_box.set_margin_end(8)
        create_box.append(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        lbl = Gtk.Label(label="Crear agente")
        lbl.set_xalign(0)
        create_box.append(lbl)
        create_btn.set_child(create_box)
        create_btn.connect("clicked", lambda _b: self._on_create(popover))
        outer.append(create_btn)

        popover.set_child(outer)
        self._popover = popover
        popover.present()

    def _build_agent_row(self, agent: dict, popover: Gtk.Popover) -> Gtk.Widget:
        agent_id = agent.get("agent_id", "")
        name = agent.get("name", "Agente")
        is_active = agent_id == self._active_agent_id

        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row_box.set_margin_top(4)
        row_box.set_margin_bottom(4)
        row_box.set_margin_start(8)
        row_box.set_margin_end(4)

        # Dot de color del agente.
        dot = Gtk.DrawingArea()
        dot.set_size_request(10, 10)
        dot.set_valign(Gtk.Align.CENTER)
        color_hex = _agent_color(agent)

        def _make_draw(c: str):
            def _draw(area, cr, w, h):
                col = c.lstrip("#")
                try:
                    r = int(col[0:2], 16) / 255
                    g = int(col[2:4], 16) / 255
                    b = int(col[4:6], 16) / 255
                except (ValueError, IndexError):
                    r, g, b = 0.31, 0.27, 0.90
                cx, cy = w / 2, h / 2
                radius = min(cx, cy)
                cr.arc(cx, cy, radius, 0, 2 * 3.14159)
                cr.set_source_rgb(r, g, b)
                cr.fill()
            return _draw

        dot.set_draw_func(_make_draw(color_hex))
        row_box.append(dot)

        # Nombre del agente.
        name_lbl = Gtk.Label(label=name)
        name_lbl.set_xalign(0)
        name_lbl.set_hexpand(True)
        if is_active:
            name_lbl.add_css_class("hermes-agent-row-name-active")
        else:
            name_lbl.add_css_class("hermes-agent-row-name")
        row_box.append(name_lbl)

        # Checkmark si está activo.
        if is_active:
            check = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
            check.add_css_class("hermes-agent-row-check")
            row_box.append(check)

        # Botón Editar.
        edit_btn = Gtk.Button.new_from_icon_name("document-edit-symbolic")
        edit_btn.set_tooltip_text("Editar")
        edit_btn.add_css_class("flat")
        edit_btn.set_valign(Gtk.Align.CENTER)
        edit_btn.connect("clicked", lambda _b, aid=agent_id: self._on_edit(aid, popover))
        row_box.append(edit_btn)

        # Botón Eliminar.
        del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        del_btn.set_tooltip_text("Eliminar")
        del_btn.add_css_class("flat")
        del_btn.add_css_class("destructive-action")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.connect("clicked", lambda _b, aid=agent_id, n=name: self._on_delete(aid, n, popover))
        row_box.append(del_btn)

        # Row clickable para seleccionar el agente.
        row_btn = Gtk.Button()
        row_btn.set_child(row_box)
        row_btn.add_css_class("hermes-agent-row-btn")
        row_btn.set_has_frame(False)
        if is_active:
            row_btn.add_css_class("active")
        row_btn.connect("clicked", lambda _b, aid=agent_id: self._on_select(aid, popover))
        return row_btn

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _on_select(self, agent_id: str, popover: Gtk.Popover) -> None:
        popover.popdown()
        self._popover = None
        if agent_id != self._active_agent_id:
            self.emit("agent-selected", agent_id)

    def _on_create(self, popover: Gtk.Popover) -> None:
        popover.popdown()
        self._popover = None
        self.emit("create-agent")

    def _on_edit(self, agent_id: str, popover: Gtk.Popover) -> None:
        popover.popdown()
        self._popover = None
        self.emit("edit-agent", agent_id)

    def _on_delete(self, agent_id: str, name: str, popover: Gtk.Popover) -> None:
        popover.popdown()
        self._popover = None
        # Diálogo de confirmación inline.
        dialog = Adw.MessageDialog(
            heading=f'Eliminar "{name}"',
            body="Esta acción no se puede deshacer. El historial de conversaciones de este agente se conservará.",
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("delete", "Eliminar")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        # Buscar la ventana padre hacia arriba en el árbol de widgets.
        parent = self.get_root()
        if isinstance(parent, Gtk.Window):
            dialog.set_transient_for(parent)

        dialog.connect(
            "response",
            lambda dlg, response, aid=agent_id: self._on_delete_confirmed(dlg, response, aid),
        )
        dialog.present()

    def _on_delete_confirmed(self, dialog, response: str, agent_id: str) -> None:
        dialog.close()
        if response == "delete":
            self.emit("delete-agent", agent_id)
