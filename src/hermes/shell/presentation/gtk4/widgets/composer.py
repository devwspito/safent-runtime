"""HermesComposer — input del usuario al agente.

Cambios US3 (spec 011):
  - Enter=enviar, Shift+Enter=salto de línea (antes Ctrl+Enter).
  - El botón "Enviar" se transforma en "Detener" (⏹) mientras hay turno.
  - Esc detiene el turno en vuelo.
  - El label de modelo es clicable → abre ProvidersDialog.
  - Pista del toolbar actualizada a "Enter para enviar".
  - on_stop_requested: callback que window.py conecta para detener el turno.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gtk  # noqa: E402

logger = logging.getLogger(__name__)


class HermesComposer(Gtk.Box):
    """Composer multi-line para chat al agente."""

    __gsignals__ = {
        "message-submitted": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # Emitido cuando el usuario pulsa Detener o Esc durante un turno.
        "stop-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        on_model_label_clicked: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("hermes-composer")

        # Indica si hay un turno en vuelo (controla el estado del botón).
        self._turn_in_flight = False

        # Callback para cuando el usuario pulsa en el label del modelo.
        self._on_model_label_clicked = on_model_label_clicked

        # Input area.
        input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        input_row.set_hexpand(True)

        self._input = Gtk.TextView()
        self._input.add_css_class("hermes-composer-input")
        self._input.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._input.set_hexpand(True)
        self._input.set_top_margin(8)
        self._input.set_bottom_margin(8)
        self._input.set_left_margin(12)
        self._input.set_right_margin(12)
        input_row.append(self._input)

        self._submit_btn = Gtk.Button.new_with_label("Enviar")
        self._submit_btn.add_css_class("hermes-primary")
        self._submit_btn.connect("clicked", self._on_action_button_clicked)
        input_row.append(self._submit_btn)

        self.append(input_row)

        # Toolbar.
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        toolbar.add_css_class("hermes-composer-toolbar")

        # Label del modelo: clicable (abre ProvidersDialog).
        self._model_btn = Gtk.Button()
        self._model_btn.add_css_class("flat")
        self._model_btn.add_css_class("hermes-composer-model-btn")
        self._model_label = Gtk.Label(label="Cargando…")
        self._model_label.add_css_class("hermes-composer-model-label")
        self._model_btn.set_child(self._model_label)
        self._model_btn.connect("clicked", self._on_model_btn_clicked)
        toolbar.append(self._model_btn)

        # Pista de teclado — actualizada a Enter=enviar.
        self._kbd_hint = Gtk.Label(label="Enter para enviar · Shift+Enter para nueva línea")
        self._kbd_hint.add_css_class("hermes-composer-hint")
        toolbar.append(self._kbd_hint)

        self.append(toolbar)

        # Cargar el modelo activo en hilo de fondo.
        threading.Thread(target=self._load_model_label, daemon=True).start()

        # Controlador de teclado.
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self._input.add_controller(key_controller)

    # ------------------------------------------------------------------
    # Estado del turno — llamado por window.py
    # ------------------------------------------------------------------

    def set_turn_in_flight(self, in_flight: bool) -> None:
        """Actualiza el estado del botón Enviar↔Detener.

        in_flight=True  → botón muestra ⏹ Detener.
        in_flight=False → botón muestra Enviar.
        """
        self._turn_in_flight = in_flight
        if in_flight:
            self._submit_btn.set_label("⏹ Detener")
            self._submit_btn.remove_css_class("hermes-primary")
            self._submit_btn.add_css_class("hermes-ghost")
        else:
            self._submit_btn.set_label("Enviar")
            self._submit_btn.remove_css_class("hermes-ghost")
            self._submit_btn.add_css_class("hermes-primary")

    # ------------------------------------------------------------------
    # Label del modelo
    # ------------------------------------------------------------------

    def _load_model_label(self) -> None:
        label = "Sin modelo · toca para configurar"
        try:
            from hermes.shell.infrastructure.shell_backend_client import (  # noqa: PLC0415
                ShellBackendClient,
            )

            providers = ShellBackendClient().list_providers()
            active = next((p for p in providers if getattr(p, "is_active", False)), None)
            if active is not None:
                label = f"Modelo: {active.default_model}"
        except Exception:  # noqa: BLE001
            logger.debug("no se pudo leer el provider activo", exc_info=True)
        GLib.idle_add(self._model_label.set_label, label)

    def refresh_model_label(self) -> None:
        """Recarga el label del modelo (llamado desde window.py cuando cambia el provider)."""
        threading.Thread(target=self._load_model_label, daemon=True).start()

    def _on_model_btn_clicked(self, _btn) -> None:
        if self._on_model_label_clicked is not None:
            self._on_model_label_clicked()

    # ------------------------------------------------------------------
    # Teclado y botón de acción
    # ------------------------------------------------------------------

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        is_enter = keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
        is_shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        is_escape = keyval == Gdk.KEY_Escape

        if is_escape and self._turn_in_flight:
            self.emit("stop-requested")
            return True

        # Enter sin Shift → enviar (o detener si hay turno).
        if is_enter and not is_shift:
            if self._turn_in_flight:
                self.emit("stop-requested")
            else:
                self._submit()
            return True

        # Shift+Enter → nueva línea (comportamiento nativo de TextView, no interceptamos).
        return False

    def _on_action_button_clicked(self, _btn) -> None:
        if self._turn_in_flight:
            self.emit("stop-requested")
        else:
            self._submit()

    def _submit(self) -> None:
        buffer = self._input.get_buffer()
        start, end = buffer.get_bounds()
        text = buffer.get_text(start, end, False).strip()
        if not text:
            return
        buffer.set_text("", 0)
        self.emit("message-submitted", text)

    def get_text(self) -> str:
        """Devuelve el texto actual del composer (sin modificarlo)."""
        buffer = self._input.get_buffer()
        start, end = buffer.get_bounds()
        return buffer.get_text(start, end, False)

    def set_text(self, text: str) -> None:
        """Establece el texto del composer (usado por chips del empty state)."""
        self._input.get_buffer().set_text(text, -1)
        # Mover el cursor al final.
        buf = self._input.get_buffer()
        buf.place_cursor(buf.get_end_iter())
        self._input.grab_focus()
