"""HermesFirstBootWizardView — conversational first-boot wizard GTK4 widget.

Conversational setup guide: the assistant walks the user through profile,
locale, network, tenant binding, consents, and exposed-services review.
State machine lives in the backend. This widget is pure presentation.

GObject signal emitted when the wizard finishes:
    wizard-finished  (no args)

Threading contract:
    All HTTP calls run in daemon threads.
    Results cross back to the GTK main loop via GLib.idle_add.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gtk  # noqa: E402

if TYPE_CHECKING:
    from hermes.shell.infrastructure.shell_backend_client import ShellBackendClient

from hermes.shell.presentation.gtk4.widgets.markdown_render import (
    render_markdown_to_pango,
)

logger = logging.getLogger(__name__)

# Wizard state -> human-readable step label (Spanish, no jargon).
_STEP_LABELS: dict[str, str] = {
    "collecting_profile": "Perfil",
    "collecting_locale": "Idioma",
    "collecting_network": "Red",
    "collecting_tenant_binding": "Vínculo",
    "collecting_consents": "Permisos",
    "reviewing_exposed_services": "Servicios",
    "finalizing": "Listo",
    "completed": "Listo",
}

_ORDERED_STEPS = [
    "collecting_profile",
    "collecting_locale",
    "collecting_network",
    "collecting_tenant_binding",
    "collecting_consents",
    "reviewing_exposed_services",
    "finalizing",
]

# Pseudo-estado del backend: el wizard pide la API key del proveedor LLM antes
# de arrancar la conversación. El texto tecleado es un secreto → se enmascara.
_AWAITING_LLM_KEY = "awaiting_llm_key"


class HermesFirstBootWizardView(Gtk.Box):
    """Full-screen conversational first-boot wizard.

    Emits ``wizard-finished`` (no args) after a successful finalize call.
    """

    __gsignals__ = {
        "wizard-finished": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, *, client: "ShellBackendClient") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("hermes-wizard-root")
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._client = client
        self._session_id: str | None = None
        self._in_flight = False
        self._awaiting_key = False

        self._build_layout()
        # Start the session as soon as the widget is created.
        self._start_session()

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        # Top: brand header.
        header = self._build_header()
        self.append(header)

        # Progress bar row.
        self._progress_row = self._build_progress_row()
        self.append(self._progress_row)

        # Middle: scrollable conversation area.
        self._chat_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        self._chat_box.add_css_class("hermes-wizard-chat")
        self._chat_box.set_hexpand(True)
        self._chat_box.set_vexpand(True)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.set_child(self._chat_box)
        # Keep scroll pinned to bottom as messages arrive.
        self._scroll = scroll
        self._vadj = scroll.get_vadjustment()
        self.append(scroll)

        # Bottom: composer.
        composer_area = self._build_composer()
        self.append(composer_area)

        # Error banner (hidden by default).
        self._error_bar = self._build_error_bar()
        self.append(self._error_bar)

    def _build_header(self) -> Gtk.Widget:
        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12
        )
        header.add_css_class("hermes-wizard-header")

        brand = Gtk.Label(label="Hermes")
        brand.add_css_class("hermes-wizard-brand")
        header.append(brand)

        subtitle = Gtk.Label(label="Configuración inicial")
        subtitle.add_css_class("hermes-wizard-subtitle")
        header.append(subtitle)

        return header

    def _build_progress_row(self) -> Gtk.Widget:
        row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        row.add_css_class("hermes-wizard-progress-row")

        self._step_labels: list[Gtk.Label] = []
        for i, state_key in enumerate(_ORDERED_STEPS[:-1]):  # omit 'finalizing'
            label_text = _STEP_LABELS.get(state_key, state_key)
            lbl = Gtk.Label(label=label_text)
            lbl.add_css_class("hermes-wizard-step")
            lbl.set_hexpand(True)
            self._step_labels.append(lbl)
            row.append(lbl)

            # Separator between steps (except last).
            if i < len(_ORDERED_STEPS) - 2:
                sep = Gtk.Label(label="›")
                sep.add_css_class("hermes-wizard-step-sep")
                row.append(sep)

        return row

    def _build_composer(self) -> Gtk.Widget:
        composer_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12
        )
        composer_box.add_css_class("hermes-wizard-composer")

        self._input = Gtk.TextView()
        self._input.add_css_class("hermes-composer-input")
        self._input.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._input.set_hexpand(True)
        self._input.set_top_margin(8)
        self._input.set_bottom_margin(8)
        self._input.set_left_margin(12)
        self._input.set_right_margin(12)
        # Disabled until session starts.
        self._input.set_sensitive(False)
        composer_box.append(self._input)

        self._send_btn = Gtk.Button.new_with_label("Responder")
        self._send_btn.add_css_class("hermes-primary")
        self._send_btn.set_sensitive(False)
        self._send_btn.connect("clicked", self._on_send_clicked)
        composer_box.append(self._send_btn)

        # Ctrl+Enter keyboard shortcut.
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self._input.add_controller(key_ctrl)

        return composer_box

    def _build_error_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12
        )
        bar.add_css_class("hermes-wizard-error-bar")
        bar.set_visible(False)

        self._error_label = Gtk.Label(label="")
        self._error_label.set_hexpand(True)
        self._error_label.set_xalign(0)
        self._error_label.set_wrap(True)
        bar.append(self._error_label)

        retry_btn = Gtk.Button.new_with_label("Reintentar")
        retry_btn.add_css_class("hermes-ghost")
        retry_btn.connect("clicked", self._on_retry_clicked)
        bar.append(retry_btn)

        return bar

    # ------------------------------------------------------------------
    # Progress indicator
    # ------------------------------------------------------------------

    def _update_progress(self, state: str) -> None:
        # Find index of the current state among the visible step labels.
        active_idx = -1
        for i, step_key in enumerate(_ORDERED_STEPS[:-1]):
            if step_key == state:
                active_idx = i
                break

        # finalizing / completed → all steps visually done.
        if state in ("finalizing", "completed"):
            active_idx = len(self._step_labels)

        for i, lbl in enumerate(self._step_labels):
            lbl.remove_css_class("hermes-wizard-step-active")
            lbl.remove_css_class("hermes-wizard-step-done")
            if active_idx >= 0 and i < active_idx:
                lbl.add_css_class("hermes-wizard-step-done")
            elif i == active_idx:
                lbl.add_css_class("hermes-wizard-step-active")

    # ------------------------------------------------------------------
    # Bubble rendering (mirrors chat_view.py exactly)
    # ------------------------------------------------------------------

    def _append_assistant_bubble(self, text: str) -> None:
        container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        container.add_css_class("hermes-chat-message-agent")
        container.set_halign(Gtk.Align.START)
        self._render_markdown_into(container, text)
        self._chat_box.append(container)
        self._scroll_to_bottom()

    def _append_user_bubble(self, text: str) -> None:
        lbl = Gtk.Label(label=text)
        lbl.set_wrap(True)
        lbl.set_xalign(0)
        lbl.set_halign(Gtk.Align.END)
        lbl.set_max_width_chars(80)
        lbl.add_css_class("hermes-chat-message-user")
        lbl.set_selectable(True)
        self._chat_box.append(lbl)
        self._scroll_to_bottom()

    def _render_markdown_into(self, container: Gtk.Box, text: str) -> None:
        while (child := container.get_first_child()) is not None:
            container.remove(child)
        for block in render_markdown_to_pango(text):
            if block.kind == "code":
                self._append_code_block(container, block.content, block.language)
            else:
                label = Gtk.Label()
                try:
                    label.set_markup(block.content)
                except Exception:
                    label.set_text(block.content)
                label.set_wrap(True)
                label.set_xalign(0)
                label.set_halign(Gtk.Align.START)
                label.set_max_width_chars(80)
                label.set_selectable(True)
                container.append(label)

    def _append_code_block(
        self, container: Gtk.Box, code: str, language: str | None
    ) -> None:
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        wrap.add_css_class("hermes-tool-card")
        if language:
            hdr = Gtk.Label(label=language)
            hdr.add_css_class("hermes-tool-card-header")
            hdr.set_xalign(0)
            wrap.append(hdr)
        body = Gtk.Label(label=code)
        body.set_xalign(0)
        body.set_wrap(True)
        body.set_selectable(True)
        body.add_css_class("hermes-code-block")
        wrap.append(body)
        container.append(wrap)

    def _scroll_to_bottom(self) -> None:
        # Schedule after layout settles so the new widget height is known.
        GLib.idle_add(self._do_scroll_to_bottom)

    def _do_scroll_to_bottom(self) -> bool:
        self._vadj.set_value(self._vadj.get_upper() - self._vadj.get_page_size())
        return False

    # ------------------------------------------------------------------
    # In-flight UI state
    # ------------------------------------------------------------------

    def _set_loading(self, loading: bool) -> None:
        self._in_flight = loading
        self._input.set_sensitive(not loading)
        self._send_btn.set_sensitive(not loading)

    def _show_error(self, message: str) -> None:
        self._error_label.set_text(message)
        self._error_bar.set_visible(True)

    def _clear_error(self) -> None:
        self._error_bar.set_visible(False)
        self._error_label.set_text("")

    # ------------------------------------------------------------------
    # Keyboard handler
    # ------------------------------------------------------------------

    def _on_key_pressed(
        self,
        _controller,
        keyval: int,
        _keycode: int,
        state,
    ) -> bool:
        is_enter = keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
        is_ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if is_enter and is_ctrl:
            self._submit()
            return True
        return False

    def _on_send_clicked(self, _btn) -> None:
        self._submit()

    def _on_retry_clicked(self, _btn) -> None:
        self._clear_error()
        if self._session_id is None:
            self._start_session()
        # else: user needs to retype; the last message was not consumed.

    def _submit(self) -> None:
        if self._in_flight or self._session_id is None:
            return
        buf = self._input.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, False).strip()
        if not text:
            return
        buf.set_text("", 0)
        self._clear_error()
        # La key del proveedor es un secreto: no la dejamos en el scrollback.
        self._append_user_bubble("••••••••" if self._awaiting_key else text)
        self._send_message(text)

    # ------------------------------------------------------------------
    # Backend calls (all blocking HTTP off main thread)
    # ------------------------------------------------------------------

    def _start_session(self) -> None:
        self._set_loading(True)
        threading.Thread(
            target=self._thread_start_session,
            daemon=True,
            name="hermes-wizard-start",
        ).start()

    def _thread_start_session(self) -> None:
        try:
            data = self._client.wizard_start()
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._on_start_error, str(exc))
            return
        GLib.idle_add(self._on_start_ok, data)

    def _on_start_ok(self, data: dict) -> bool:
        self._session_id = data["session_id"]
        state = data.get("state", "collecting_profile")
        assistant_msg = data.get("assistant_message", "")
        self._awaiting_key = state == _AWAITING_LLM_KEY
        self._update_progress(state)
        self._append_assistant_bubble(assistant_msg)
        self._set_loading(False)
        return False

    def _on_start_error(self, error: str) -> bool:
        logger.error("wizard start error: %s", error)
        self._set_loading(False)
        self._show_error(
            f"No se pudo iniciar la configuración: {error}. "
            "Asegúrate de que el servidor esté activo."
        )
        return False

    def _send_message(self, text: str) -> None:
        self._set_loading(True)
        threading.Thread(
            target=self._thread_send_message,
            args=(self._session_id, text),
            daemon=True,
            name="hermes-wizard-msg",
        ).start()

    def _thread_send_message(self, session_id: str, text: str) -> None:
        try:
            data = self._client.wizard_send(session_id=session_id, msg=text)
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._on_message_error, str(exc))
            return
        GLib.idle_add(self._on_message_ok, data)

    def _on_message_ok(self, data: dict) -> bool:
        state = data.get("state", "")
        assistant_msg = data.get("assistant_message", "")
        done = data.get("done", False)

        self._awaiting_key = state == _AWAITING_LLM_KEY
        self._update_progress(state)

        if assistant_msg:
            self._append_assistant_bubble(assistant_msg)

        if state == "finalizing":
            # Backend instructs us to finalize.
            self._set_loading(True)
            threading.Thread(
                target=self._thread_finalize,
                args=(self._session_id,),
                daemon=True,
                name="hermes-wizard-finalize",
            ).start()
            return False

        if done:
            self._on_wizard_complete()
            return False

        self._set_loading(False)
        return False

    def _on_message_error(self, error: str) -> bool:
        logger.error("wizard message error: %s", error)
        self._set_loading(False)
        self._show_error(
            f"Error al procesar tu respuesta: {error}. Puedes reintentar."
        )
        return False

    def _thread_finalize(self, session_id: str) -> None:
        try:
            self._client.wizard_finalize(session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._on_finalize_error, str(exc))
            return
        GLib.idle_add(self._on_finalize_ok)

    def _on_finalize_ok(self) -> bool:
        self._update_progress("completed")
        self._append_assistant_bubble(
            "**Todo listo.** Tu entorno Hermes está configurado y listo para trabajar."
        )
        self._set_loading(False)
        # Give the user a moment to read the completion message, then transition.
        GLib.timeout_add(1800, self._emit_finished)
        return False

    def _on_finalize_error(self, error: str) -> bool:
        logger.error("wizard finalize error: %s", error)
        self._set_loading(False)
        self._show_error(
            f"No se pudo completar la configuración: {error}. Intenta de nuevo."
        )
        return False

    def _on_wizard_complete(self) -> None:
        """Called when done==True but state is not 'finalizing' (backend already finalized)."""
        self._update_progress("completed")
        self._set_loading(False)
        GLib.timeout_add(1800, self._emit_finished)

    def _emit_finished(self) -> bool:
        self.emit("wizard-finished")
        return False
