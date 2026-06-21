"""AgentDialog — diálogo para crear o editar un agente.

Campos: Nombre, Color, Rol, Tono, Misión, Instrucciones (multilínea),
        Nivel de autonomía.
Guardar emite el callback on_save con el draft dict.

Sigue el mismo patrón que AddProviderDialog (providers_dialog.py):
  - Adw.Window modal
  - Adw.ToolbarView + Adw.HeaderBar
  - Adw.PreferencesPage / Adw.PreferencesGroup / Adw.EntryRow
  - Callback on_save en lugar de señal GObject (más simple para diálogos)

Restricciones:
  - Sin jerga IA en etiquetas visibles.
  - Color se elige de una lista predefinida legible.
  - En modo edición, rellena los campos con los valores actuales.
"""

from __future__ import annotations

import logging
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

logger = logging.getLogger(__name__)

# Opciones de color (label visible, valor guardado en el draft).
_COLOR_OPTIONS: list[tuple[str, str]] = [
    ("Índigo", "indigo"),
    ("Azul", "blue"),
    ("Verde", "green"),
    ("Ámbar", "amber"),
    ("Rojo", "red"),
    ("Morado", "purple"),
    ("Rosa", "pink"),
    ("Verde azulado", "teal"),
    ("Gris", "gray"),
]

# Opciones de tono (label, valor).
_REGISTER_OPTIONS: list[tuple[str, str]] = [
    ("Formal", "formal"),
    ("Profesional", "professional"),
    ("Amigable", "friendly"),
    ("Conciso", "concise"),
    ("Técnico", "technical"),
]

# Opciones de nivel de autonomía (label, valor de draft, subtítulo).
# El valor coincide con AutonomyLevel.value — misma clave que usa serialization.py.
_AUTONOMY_OPTIONS: list[tuple[str, str, str]] = [
    (
        "Pregunta siempre",
        "ask_always",
        "Te pide permiso antes de cualquier acción con efecto fuera del chat.",
    ),
    (
        "Equilibrado",
        "balanced",
        "Actúa solo en lo reversible; te pide permiso para lo irreversible o externo.",
    ),
    (
        "Autónomo",
        "autonomous",
        "Trabaja por su cuenta; solo te consulta lo irreversible de alto riesgo.",
    ),
]

# Índice de la opción "Autónomo" — referenciado por el guardarraíl de seguridad.
_AUTONOMOUS_IDX: int = next(
    i for i, (_, v, _s) in enumerate(_AUTONOMY_OPTIONS) if v == "autonomous"
)
_BALANCED_IDX: int = next(
    i for i, (_, v, _s) in enumerate(_AUTONOMY_OPTIONS) if v == "balanced"
)


def _index_of(options: list[tuple[str, str]], value: str, default: int = 0) -> int:
    for i, (_, v) in enumerate(options):
        if v == value:
            return i
    return default


def autonomy_value_for_index(idx: int) -> str:
    """Devuelve el valor de draft para un índice del selector de autonomía.

    Función pura: testable sin GTK. Cae a 'balanced' si el índice está fuera de rango.
    """
    if 0 <= idx < len(_AUTONOMY_OPTIONS):
        return _AUTONOMY_OPTIONS[idx][1]
    return "balanced"


def autonomy_index_for_value(value: str) -> int:
    """Devuelve el índice del selector para un valor de autonomy_level.

    Función pura: testable sin GTK. Cae a _BALANCED_IDX si el valor no existe.
    """
    for i, (_, v, _s) in enumerate(_AUTONOMY_OPTIONS):
        if v == value:
            return i
    return _BALANCED_IDX


class AgentDialog(Adw.Window):
    """Formulario crear / editar agente."""

    def __init__(
        self,
        *,
        parent: Gtk.Window,
        on_save: Callable[[dict], None],
        agent: dict | None = None,
    ) -> None:
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(560, 720)
        self._on_save = on_save
        self._is_edit = agent is not None
        self._agent = agent or {}

        title = "Editar agente" if self._is_edit else "Crear agente"
        self.set_title(title)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        cancel_btn = Gtk.Button.new_with_label("Cancelar")
        cancel_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel_btn)

        save_btn = Gtk.Button.new_with_label("Guardar")
        save_btn.add_css_class("hermes-primary")
        save_btn.connect("clicked", lambda _b: self._save())
        header.pack_end(save_btn)

        # ---- Formulario ----
        page = Adw.PreferencesPage()

        # Grupo Identidad.
        identity_group = Adw.PreferencesGroup()
        identity_group.set_title("Identidad")

        self._name_entry = Adw.EntryRow()
        self._name_entry.set_title("Nombre")
        self._name_entry.set_text(self._agent.get("name", ""))
        identity_group.add(self._name_entry)

        # Color — DropDown.
        color_labels = [c[0] for c in _COLOR_OPTIONS]
        self._color_combo = Gtk.DropDown.new_from_strings(color_labels)
        current_color_idx = _index_of(_COLOR_OPTIONS, self._agent.get("color", "indigo"))
        self._color_combo.set_selected(current_color_idx)

        color_row = Adw.ActionRow()
        color_row.set_title("Color")
        color_row.add_suffix(self._color_combo)
        color_row.set_activatable_widget(self._color_combo)
        identity_group.add(color_row)

        # Tono — DropDown.
        register_labels = [r[0] for r in _REGISTER_OPTIONS]
        self._register_combo = Gtk.DropDown.new_from_strings(register_labels)
        current_register_idx = _index_of(_REGISTER_OPTIONS, self._agent.get("register", "professional"))
        self._register_combo.set_selected(current_register_idx)

        register_row = Adw.ActionRow()
        register_row.set_title("Tono")
        register_row.add_suffix(self._register_combo)
        register_row.set_activatable_widget(self._register_combo)
        identity_group.add(register_row)

        page.add(identity_group)

        # Grupo Comportamiento.
        behavior_group = Adw.PreferencesGroup()
        behavior_group.set_title("Comportamiento")

        self._role_entry = Adw.EntryRow()
        self._role_entry.set_title("Rol")
        self._role_entry.set_text(self._agent.get("role", ""))
        behavior_group.add(self._role_entry)

        self._mission_entry = Adw.EntryRow()
        self._mission_entry.set_title("Misión")
        self._mission_entry.set_text(self._agent.get("primary_mission", ""))
        behavior_group.add(self._mission_entry)

        page.add(behavior_group)

        # Grupo Instrucciones (TextView multilínea en un ActionRow sin label de campo).
        instructions_group = Adw.PreferencesGroup()
        instructions_group.set_title("Instrucciones")
        instructions_group.set_description(
            "Contexto adicional que el agente tendrá siempre presente."
        )

        # Frame + TextView (Adw.EntryRow no soporta multilínea).
        frame = Gtk.Frame()
        frame.add_css_class("hermes-instructions-frame")
        frame.set_margin_top(4)
        frame.set_margin_bottom(4)

        self._instructions_tv = Gtk.TextView()
        self._instructions_tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._instructions_tv.set_left_margin(12)
        self._instructions_tv.set_right_margin(12)
        self._instructions_tv.set_top_margin(10)
        self._instructions_tv.set_bottom_margin(10)
        self._instructions_tv.add_css_class("hermes-instructions-text")
        buf = self._instructions_tv.get_buffer()
        buf.set_text(self._agent.get("instructions", ""))

        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(120)
        scroll.set_child(self._instructions_tv)
        frame.set_child(scroll)

        instructions_group.add(frame)
        page.add(instructions_group)

        # Grupo Nivel de autonomía.
        autonomy_group = Adw.PreferencesGroup()
        autonomy_group.set_title("Nivel de autonomía")
        autonomy_group.set_description(
            "Controla cuándo el agente actúa por su cuenta y cuándo te pide permiso."
        )

        autonomy_labels = [opt[0] for opt in _AUTONOMY_OPTIONS]
        self._autonomy_combo = Gtk.DropDown.new_from_strings(autonomy_labels)

        current_autonomy_value = self._agent.get("autonomy_level", "balanced")
        current_autonomy_idx = autonomy_index_for_value(current_autonomy_value)
        self._autonomy_combo.set_selected(current_autonomy_idx)

        # Subtítulo dinámico — describe el nivel seleccionado en cada momento.
        self._autonomy_row = Adw.ActionRow()
        self._autonomy_row.set_title("Modo")
        self._autonomy_row.set_subtitle(_AUTONOMY_OPTIONS[current_autonomy_idx][2])
        self._autonomy_row.add_suffix(self._autonomy_combo)
        self._autonomy_row.set_activatable_widget(self._autonomy_combo)
        autonomy_group.add(self._autonomy_row)

        page.add(autonomy_group)

        toolbar.set_content(page)
        self.set_content(toolbar)

        # Guardarraíl: confirmar antes de activar modo autónomo.
        # Se conecta DESPUÉS de set_selected para que no dispare en la carga inicial.
        self._autonomy_guard_active = False  # evita bucle en el revert programático
        self._autonomy_prev_idx = current_autonomy_idx
        self._autonomy_combo.connect("notify::selected", self._on_autonomy_changed)

    # ------------------------------------------------------------------
    # Guardarraíl de autonomía
    # ------------------------------------------------------------------

    def _on_autonomy_changed(self, combo: Gtk.DropDown, _param: object) -> None:
        """Intercepta cambios en el selector de autonomía.

        Si el usuario elige "Autónomo" muestra un AlertDialog de confirmación.
        Si cancela, revierte el selector al valor anterior sin disparar de nuevo
        este handler (guard _autonomy_guard_active).
        Cualquier otro cambio (bajar autonomía) se acepta directamente.
        """
        if self._autonomy_guard_active:
            return

        new_idx = combo.get_selected()

        # Actualiza el subtítulo de la fila con la descripción del nivel elegido.
        if 0 <= new_idx < len(_AUTONOMY_OPTIONS):
            self._autonomy_row.set_subtitle(_AUTONOMY_OPTIONS[new_idx][2])

        if new_idx != _AUTONOMOUS_IDX:
            # Bajar autonomía no requiere confirmación.
            self._autonomy_prev_idx = new_idx
            return

        # El usuario eligió "Autónomo" — pedir confirmación.
        prev_idx = self._autonomy_prev_idx

        dialog = Adw.AlertDialog()
        dialog.set_heading("¿Activar modo autónomo?")
        dialog.set_body(
            "Tu asistente actuará sin pedirte permiso para más acciones. "
            "Las acciones de alto riesgo o irreversibles seguirán necesitando tu aprobación."
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("activate", "Activar")
        dialog.set_response_appearance("activate", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(d: Adw.AlertDialog, response: str) -> None:
            if response == "activate":
                self._autonomy_prev_idx = _AUTONOMOUS_IDX
                return
            # Cancelado: revertir el selector al valor anterior sin re-disparar el handler.
            self._autonomy_guard_active = True
            try:
                self._autonomy_combo.set_selected(prev_idx)
                if 0 <= prev_idx < len(_AUTONOMY_OPTIONS):
                    self._autonomy_row.set_subtitle(_AUTONOMY_OPTIONS[prev_idx][2])
            finally:
                self._autonomy_guard_active = False

        dialog.connect("response", _on_response)
        dialog.present(self)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save(self) -> None:
        name = self._name_entry.get_text().strip()
        if not name:
            # El nombre es obligatorio — resalta el campo.
            self._name_entry.add_css_class("error")
            return
        self._name_entry.remove_css_class("error")

        color_idx = self._color_combo.get_selected()
        color = _COLOR_OPTIONS[color_idx][1] if 0 <= color_idx < len(_COLOR_OPTIONS) else "indigo"

        register_idx = self._register_combo.get_selected()
        register = _REGISTER_OPTIONS[register_idx][1] if 0 <= register_idx < len(_REGISTER_OPTIONS) else "professional"

        autonomy_idx = self._autonomy_combo.get_selected()
        autonomy_level = autonomy_value_for_index(autonomy_idx)

        buf = self._instructions_tv.get_buffer()
        instructions = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

        draft = {
            "name": name,
            "color": color,
            "role": self._role_entry.get_text().strip(),
            "register": register,
            "primary_mission": self._mission_entry.get_text().strip(),
            "instructions": instructions,
            "autonomy_level": autonomy_level,
            "language": self._agent.get("language", "es"),
            "golden_rules": self._agent.get("golden_rules", []),
            "forbidden_phrases": self._agent.get("forbidden_phrases", []),
        }
        self._on_save(draft)
        self.close()
