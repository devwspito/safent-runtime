"""ProvidersDialog — Settings -> Modelos & Proveedores.

UI para gestionar providers LLM: add, edit, delete, test, activate,
auto-detect local.
"""

from __future__ import annotations

import logging
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from hermes.shell.infrastructure.shell_backend_client import (
    ProviderDTO,
    ShellBackendClient,
)

logger = logging.getLogger(__name__)


# (label, kind_value, default_base_url, ejemplo_modelo)
_KIND_CHOICES: list[tuple[str, str, str, str]] = [
    ("vLLM (self-hosted)", "vllm", "http://localhost:8000/v1", "qwen3-coder-35b"),
    ("Ollama (local)", "ollama", "http://localhost:11434", "qwen3:35b"),
    ("LM Studio", "lm_studio", "http://localhost:1234/v1", "qwen3"),
    ("Anthropic Claude", "anthropic", "", "claude-opus-4-7"),
    ("OpenAI", "openai", "", "gpt-5"),
    ("Gemini", "gemini", "", "gemini-2.5-pro"),
    ("Azure OpenAI", "azure_openai", "https://YOUR.openai.azure.com", "gpt-5"),
    ("Mistral", "mistral", "", "mistral-large-2"),
    ("Groq", "groq", "", "llama-3.3-70b"),
    ("Deepseek (CN)", "deepseek", "", "deepseek-chat"),
    ("Moonshot Kimi (CN)", "moonshot", "", "moonshot-v1-128k"),
    ("Zhipu GLM (CN)", "zhipu", "", "glm-4.6"),
    ("Doubao (CN)", "doubao", "", "doubao-pro-256k"),
    ("Qwen Dashscope (CN)", "qwen_dashscope", "", "qwen-max"),
    ("OpenRouter (proxy)", "openrouter", "", "anthropic/claude-3.5-sonnet"),
    ("Together AI", "together", "", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    ("OpenAI-compatible", "openai_compatible", "http://localhost:8080/v1", "model"),
]


class ProvidersDialog(Adw.Window):
    """Dialog modal con lista de providers + acciones."""

    def __init__(
        self,
        *,
        parent: Gtk.Window,
        client: ShellBackendClient,
        on_active_changed: Callable[[ProviderDTO | None], None] | None = None,
    ) -> None:
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(720, 560)
        self.set_title("Modelos y proveedores")

        self._client = client
        self._on_active_changed = on_active_changed

        # Container principal.
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        # Botones del header.
        self._auto_btn = Gtk.Button.new_with_label("Auto-detectar")
        self._auto_btn.add_css_class("hermes-ghost")
        self._auto_btn.connect("clicked", lambda _b: self._auto_detect())
        header.pack_start(self._auto_btn)

        self._add_btn = Gtk.Button.new_with_label("+ Añadir")
        self._add_btn.add_css_class("hermes-primary")
        self._add_btn.connect("clicked", lambda _b: self._open_add_dialog())
        header.pack_end(self._add_btn)

        # Lista.
        self._toast = Adw.ToastOverlay()

        # Outer box that holds either the list scroll or the empty state.
        self._content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._content_box.set_vexpand(True)
        self._empty_state: Gtk.Widget | None = None

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_top(16)
        self._list_box.set_margin_bottom(16)
        self._list_box.set_margin_start(16)
        self._list_box.set_margin_end(16)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_child(self._list_box)
        self._scroll.set_vexpand(True)
        self._content_box.append(self._scroll)
        self._toast.set_child(self._content_box)

        toolbar.set_content(self._toast)
        self.set_content(toolbar)

        self._refresh()

    # ----------------------------------------------------------------
    # Refresh
    # ----------------------------------------------------------------
    def _refresh(self) -> None:
        # Vaciar lista y limpiar estado vacío previo.
        while (child := self._list_box.get_first_child()) is not None:
            self._list_box.remove(child)
        if self._empty_state is not None:
            self._content_box.remove(self._empty_state)
            self._empty_state = None

        try:
            providers = self._client.list_providers()
        except Exception as exc:  # noqa: BLE001
            self._show_toast(f"Error: {exc}")
            return

        if not providers:
            self._scroll.set_visible(False)
            empty = Adw.StatusPage()
            empty.set_icon_name("network-server-symbolic")
            empty.set_title("Conecta tu primer modelo de IA")
            empty.set_description(
                "Usa tu propia clave (OpenAI, Anthropic) o una instancia local. "
                "Sin lock-in."
            )
            empty.set_vexpand(True)
            cta = Gtk.Button.new_with_label("Añadir proveedor")
            cta.add_css_class("hermes-primary")
            cta.set_halign(Gtk.Align.CENTER)
            cta.connect("clicked", lambda _b: self._open_add_dialog())
            empty.set_child(cta)
            self._empty_state = empty
            self._content_box.append(self._empty_state)
            return

        self._scroll.set_visible(True)
        for p in providers:
            self._list_box.append(self._build_row(p))

    def _build_row(self, p: ProviderDTO) -> Gtk.Widget:
        row = Adw.ActionRow()
        row.set_title(p.alias)

        subtitle_parts = [p.kind, p.default_model]
        if p.base_url:
            subtitle_parts.append(p.base_url)
        if p.has_api_key:
            subtitle_parts.append("🔐 key")
        row.set_subtitle(" · ".join(subtitle_parts))

        # Dot de conectividad.
        dot = Gtk.Image()
        if p.connectivity == "reachable":
            dot.set_from_icon_name("emblem-ok-symbolic")
            dot.add_css_class("success")
        elif p.connectivity == "unreachable":
            dot.set_from_icon_name("dialog-warning-symbolic")
            dot.add_css_class("warning")
        else:
            dot.set_from_icon_name("dialog-question-symbolic")
        row.add_prefix(dot)

        # Acciones.
        if p.is_active:
            active_pill = Gtk.Label.new("ACTIVO")
            active_pill.add_css_class("hermes-badge")
            active_pill.add_css_class("accent")
            active_pill.set_valign(Gtk.Align.CENTER)
            row.add_suffix(active_pill)
        else:
            activate_btn = Gtk.Button.new_with_label("Activar")
            activate_btn.set_valign(Gtk.Align.CENTER)
            activate_btn.add_css_class("hermes-ghost")
            activate_btn.connect(
                "clicked", lambda _b, pid=p.provider_id: self._activate(pid)
            )
            row.add_suffix(activate_btn)

        test_btn = Gtk.Button.new_from_icon_name("network-transmit-receive-symbolic")
        test_btn.set_tooltip_text("Test connection")
        test_btn.set_valign(Gtk.Align.CENTER)
        test_btn.add_css_class("flat")
        test_btn.connect("clicked", lambda _b, pid=p.provider_id: self._test(pid))
        row.add_suffix(test_btn)

        delete_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        delete_btn.set_tooltip_text("Eliminar")
        delete_btn.set_valign(Gtk.Align.CENTER)
        delete_btn.add_css_class("flat")
        delete_btn.add_css_class("destructive-action")
        delete_btn.connect("clicked", lambda _b, pid=p.provider_id: self._delete(pid))
        row.add_suffix(delete_btn)

        return row

    # ----------------------------------------------------------------
    # Actions
    # ----------------------------------------------------------------
    def _activate(self, provider_id: str) -> None:
        try:
            p = self._client.activate_provider(provider_id=provider_id)
        except Exception as exc:  # noqa: BLE001
            self._show_toast(f"Error: {exc}")
            return
        self._show_toast(f"'{p.alias}' activo")
        if self._on_active_changed is not None:
            self._on_active_changed(p)
        self._refresh()

    def _test(self, provider_id: str) -> None:
        self._show_toast("Probando conexión…")

        def _run() -> None:
            try:
                result = self._client.test_provider(provider_id=provider_id)
                msg = (
                    "✓ Conexión OK"
                    if result.get("ok")
                    else f"✗ {result.get('error', 'failed')}"
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"✗ {exc}"
            GLib.idle_add(self._show_toast, msg)
            GLib.idle_add(self._refresh)

        import threading

        threading.Thread(target=_run, daemon=True).start()

    def _delete(self, provider_id: str) -> None:
        try:
            self._client.delete_provider(provider_id=provider_id)
        except Exception as exc:  # noqa: BLE001
            self._show_toast(f"Error: {exc}")
            return
        self._show_toast("Proveedor eliminado")
        self._refresh()

    def _auto_detect(self) -> None:
        self._show_toast("Buscando providers locales…")

        def _run() -> None:
            try:
                found = self._client.auto_detect_local()
                msg = (
                    f"Detectados: {len(found)} ({', '.join(p.alias for p in found)})"
                    if found
                    else "Sin providers locales detectados"
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"Error: {exc}"
            GLib.idle_add(self._show_toast, msg)
            GLib.idle_add(self._refresh)

        import threading

        threading.Thread(target=_run, daemon=True).start()

    def _open_add_dialog(self) -> None:
        dlg = AddProviderDialog(parent=self, on_save=self._save_new)
        dlg.present()

    def _save_new(
        self,
        *,
        alias: str,
        kind: str,
        default_model: str,
        base_url: str,
        api_key: str,
        set_active: bool,
    ) -> None:
        try:
            self._client.create_provider(
                alias=alias,
                kind=kind,
                default_model=default_model,
                base_url=base_url or None,
                api_key=api_key or None,
                set_active=set_active,
            )
        except Exception as exc:  # noqa: BLE001
            self._show_toast(f"Error: {exc}")
            return
        self._show_toast(f"'{alias}' añadido")
        self._refresh()

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------
    def _show_toast(self, msg: str) -> bool:
        toast = Adw.Toast.new(msg)
        toast.set_timeout(3)
        self._toast.add_toast(toast)
        return False


class AddProviderDialog(Adw.Window):
    """Formulario para crear un provider nuevo."""

    def __init__(
        self,
        *,
        parent: Gtk.Window,
        on_save: Callable[..., None],
    ) -> None:
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(520, 540)
        self.set_title("Añadir proveedor")
        self._on_save = on_save

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

        # Form.
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        group.set_title("Proveedor")

        # Kind selector — usando ComboRow para evitar problemas con StringList.
        kinds = [c[0] for c in _KIND_CHOICES]
        self._kind_combo = Gtk.DropDown.new_from_strings(kinds)
        self._kind_combo.set_selected(0)
        self._kind_combo.connect("notify::selected", self._on_kind_changed)

        kind_row = Adw.ActionRow()
        kind_row.set_title("Tipo")
        kind_row.add_suffix(self._kind_combo)
        kind_row.set_activatable_widget(self._kind_combo)
        group.add(kind_row)

        # Alias.
        self._alias_entry = Adw.EntryRow()
        self._alias_entry.set_title("Alias (nombre para ti)")
        self._alias_entry.set_text(_KIND_CHOICES[0][0])
        group.add(self._alias_entry)

        # Base URL — solo para instancias con endpoint propio (local/Azure/compat).
        # Cloud (OpenAI/Anthropic/…) lo conoce LiteLLM -> campo oculto.
        self._base_url_entry = Adw.EntryRow()
        self._base_url_entry.set_title("Endpoint (base URL)")
        self._base_url_entry.set_text(_KIND_CHOICES[0][2])
        self._base_url_entry.set_visible(bool(_KIND_CHOICES[0][2]))
        group.add(self._base_url_entry)

        # Model.
        self._model_entry = Adw.EntryRow()
        self._model_entry.set_title("Modelo")
        self._model_entry.set_text(_KIND_CHOICES[0][3])
        group.add(self._model_entry)

        # API key (password).
        self._api_key_entry = Adw.PasswordEntryRow()
        self._api_key_entry.set_title("API key")
        group.add(self._api_key_entry)

        # Set active toggle.
        self._set_active_switch = Adw.SwitchRow()
        self._set_active_switch.set_title("Activar al guardar")
        self._set_active_switch.set_active(True)
        group.add(self._set_active_switch)

        page.add(group)
        toolbar.set_content(page)
        self.set_content(toolbar)

    def _on_kind_changed(self, *_args) -> None:
        idx = self._kind_combo.get_selected()
        if idx < 0 or idx >= len(_KIND_CHOICES):
            return
        label, _kind, base, model = _KIND_CHOICES[idx]
        self._alias_entry.set_text(label)
        self._base_url_entry.set_text(base)
        self._base_url_entry.set_visible(bool(base))
        self._model_entry.set_text(model)

    def _save(self) -> None:
        idx = self._kind_combo.get_selected()
        if idx < 0 or idx >= len(_KIND_CHOICES):
            return
        _label, kind, _base, _model = _KIND_CHOICES[idx]
        self._on_save(
            alias=self._alias_entry.get_text().strip(),
            kind=kind,
            default_model=self._model_entry.get_text().strip(),
            base_url=self._base_url_entry.get_text().strip(),
            api_key=self._api_key_entry.get_text(),
            set_active=self._set_active_switch.get_active(),
        )
        self.close()
