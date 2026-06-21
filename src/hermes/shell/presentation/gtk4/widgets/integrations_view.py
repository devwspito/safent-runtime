"""HermesIntegrationsView — Composio integration catalogue + connected accounts.

Presents two logical states:
  1. No key stored  → friendly empty-state with PasswordEntryRow to save the
                       Composio API key and a LinkButton to composio.dev.
  2. Key present    → two sections:
       (a) "Conectadas"  — list of active connected accounts + Disconnect button
       (b) "Catálogo"    — searchable toolkit catalogue + Connect button that
                           opens the OAuth redirect URL in the system browser.

Threading contract (same as skills_view.py):
    All HTTP calls run in daemon threads.
    Results cross back to the GTK main loop via GLib.idle_add.
"""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from hermes.shell.infrastructure.shell_backend_client import ShellBackendClient  # noqa: E402

logger = logging.getLogger(__name__)

_COMPOSIO_URL = "https://app.composio.dev"
_DESC_MAX_LEN = 120

# Human-readable connection status labels.
_STATUS_LABELS: dict[str, str] = {
    "ACTIVE": "Activa",
    "INITIATED": "En progreso",
    "FAILED": "Error",
    "EXPIRED": "Expirada",
}


class HermesIntegrationsView(Gtk.Box):
    """Panel de Integraciones — catálogo Composio + cuentas conectadas."""

    def __init__(self, *, client: ShellBackendClient) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._client = client
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_vexpand(True)
        self._toast_overlay.set_hexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_hexpand(True)
        inner.set_vexpand(True)
        inner.append(self._build_toolbar())

        # The content switcher flips between the "no-key" state page and the
        # "has-key" scrollable content.
        self._content_stack = Gtk.Stack()
        self._content_stack.set_vexpand(True)
        self._content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._content_stack.set_transition_duration(180)

        self._content_stack.add_named(self._build_no_key_page(), "no_key")
        self._content_stack.add_named(self._build_has_key_page(), "has_key")

        inner.append(self._content_stack)
        self._toast_overlay.set_child(inner)
        self.append(self._toast_overlay)

        self._reload()

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------

    def _build_toolbar(self) -> Gtk.Widget:
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.add_css_class("hermes-view-toolbar")

        title = Gtk.Label(label="Integraciones")
        title.add_css_class("hermes-page-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        toolbar.append(title)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Buscar apps…")
        self._search_entry.set_size_request(220, -1)
        self._search_entry.connect("search-changed", self._on_search_changed)
        toolbar.append(self._search_entry)

        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text("Refrescar")
        refresh_btn.connect("clicked", lambda _b: self._reload())
        toolbar.append(refresh_btn)

        return toolbar

    # ------------------------------------------------------------------
    # No-key page (empty state + key entry)
    # ------------------------------------------------------------------

    def _build_no_key_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(560)
        clamp.set_margin_top(32)
        clamp.set_margin_bottom(32)
        clamp.set_margin_start(24)
        clamp.set_margin_end(24)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)

        # Status page as the hero illustration.
        status = Adw.StatusPage()
        status.set_icon_name("network-transmit-receive-symbolic")
        status.set_title("Conecta tus apps")
        status.set_description(
            "Composio gestiona el acceso seguro a Gmail, Drive, Outlook, Slack "
            "y 1000+ apps. Pega tu API key para empezar."
        )
        box.append(status)

        link_btn = Gtk.LinkButton.new_with_label(_COMPOSIO_URL, "Obtener API key en composio.dev")
        link_btn.set_halign(Gtk.Align.CENTER)
        box.append(link_btn)

        # Key entry form.
        group = Adw.PreferencesGroup()
        group.set_title("API key de Composio")

        self._key_entry = Adw.PasswordEntryRow()
        self._key_entry.set_title("API key")
        self._key_entry.connect("entry-activated", lambda _r: self._save_key())
        group.add(self._key_entry)

        box.append(group)

        self._save_key_btn = Gtk.Button.new_with_label("Guardar key")
        self._save_key_btn.add_css_class("hermes-primary")
        self._save_key_btn.set_halign(Gtk.Align.END)
        self._save_key_btn.connect("clicked", lambda _b: self._save_key())
        box.append(self._save_key_btn)

        clamp.set_child(box)
        scroll.set_child(clamp)
        return scroll

    # ------------------------------------------------------------------
    # Has-key page (connected + catalogue)
    # ------------------------------------------------------------------

    def _build_has_key_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(720)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(24)
        clamp.set_margin_end(24)

        self._has_key_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)

        # Section A — connected accounts.
        self._connected_group = Adw.PreferencesGroup()
        self._connected_group.set_title("Conectadas")
        self._connected_group.set_description("Apps con acceso activo")
        self._has_key_box.append(self._connected_group)
        self._connected_rows: list[Gtk.Widget] = []

        # Section B — catalogue.
        self._catalogue_group = Adw.PreferencesGroup()
        self._catalogue_group.set_title("Catálogo")
        self._catalogue_group.set_description(
            "Conecta cualquier app; Composio gestiona el acceso OAuth de forma segura."
        )
        self._has_key_box.append(self._catalogue_group)
        self._catalogue_rows: list[Gtk.Widget] = []

        # Loading placeholder for catalogue.
        self._catalogue_loading = Gtk.Label(label="Cargando catálogo…")
        self._catalogue_loading.add_css_class("hermes-wizard-form-subheading")
        self._catalogue_loading.set_xalign(0)
        self._catalogue_loading.set_margin_start(4)
        self._has_key_box.append(self._catalogue_loading)

        clamp.set_child(self._has_key_box)
        scroll.set_child(clamp)
        return scroll

    # ------------------------------------------------------------------
    # Reload — fetch status → decide which page to show
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        def runner() -> None:
            try:
                status = self._client.composio_status()
            except Exception as exc:  # noqa: BLE001
                logger.warning("integrations: composio_status: %s", exc)
                status = {"has_key": False, "entity_id": "default"}
            GLib.idle_add(lambda: self._on_status_loaded(status))

        threading.Thread(target=runner, daemon=True, name="hermes-integrations-status").start()

    def _on_status_loaded(self, status: dict) -> bool:
        if status.get("has_key"):
            self._content_stack.set_visible_child_name("has_key")
            self._load_connected()
            self._load_catalogue(search="")
        else:
            self._content_stack.set_visible_child_name("no_key")
        return False

    # ------------------------------------------------------------------
    # Connected accounts — load + render
    # ------------------------------------------------------------------

    def _load_connected(self) -> None:
        def runner() -> None:
            try:
                accounts = self._client.composio_connected()
            except Exception as exc:  # noqa: BLE001
                logger.warning("integrations: composio_connected: %s", exc)
                accounts = []
            GLib.idle_add(lambda: self._render_connected(accounts))

        threading.Thread(
            target=runner, daemon=True, name="hermes-integrations-connected"
        ).start()

    def _render_connected(self, accounts: list[dict]) -> bool:
        for row in self._connected_rows:
            self._connected_group.remove(row)
        self._connected_rows = []

        if not accounts:
            row = Adw.ActionRow()
            row.set_title("Ninguna app conectada aún")
            row.set_subtitle("Conecta una app desde el catálogo de abajo")
            self._connected_group.add(row)
            self._connected_rows.append(row)
            return False

        for acc in accounts:
            row = self._build_connected_row(acc)
            self._connected_group.add(row)
            self._connected_rows.append(row)

        return False

    def _build_connected_row(self, acc: dict) -> Gtk.Widget:
        row = Adw.ActionRow()
        slug = acc.get("toolkit_slug", acc.get("app_name", ""))
        row.set_title(slug)

        raw_status = acc.get("status", "")
        status_label = _STATUS_LABELS.get(raw_status, raw_status)
        row.set_subtitle(f"Estado: {status_label}  ·  {acc.get('entity_id', '')}")

        disc_btn = Gtk.Button.new_with_label("Desconectar")
        disc_btn.add_css_class("flat")
        disc_btn.add_css_class("destructive-action")
        disc_btn.set_valign(Gtk.Align.CENTER)
        disc_btn.set_tooltip_text("Revocar el acceso de esta app")
        acc_id = acc.get("id", "")
        disc_btn.connect("clicked", lambda _b, aid=acc_id: self._disconnect(aid))
        row.add_suffix(disc_btn)

        return row

    # ------------------------------------------------------------------
    # Toolkit catalogue — load + render
    # ------------------------------------------------------------------

    def _load_catalogue(self, *, search: str) -> None:
        self._catalogue_loading.set_visible(True)

        def runner() -> None:
            try:
                toolkits = self._client.composio_toolkits(search=search)
            except Exception as exc:  # noqa: BLE001
                logger.warning("integrations: composio_toolkits: %s", exc)
                toolkits = []
            GLib.idle_add(lambda: self._render_catalogue(toolkits))

        threading.Thread(
            target=runner, daemon=True, name="hermes-integrations-catalogue"
        ).start()

    def _render_catalogue(self, toolkits: list[dict]) -> bool:
        for row in self._catalogue_rows:
            self._catalogue_group.remove(row)
        self._catalogue_rows = []
        self._catalogue_loading.set_visible(False)

        if not toolkits:
            row = Adw.ActionRow()
            row.set_title("Sin resultados")
            row.set_subtitle("Prueba con otro término de búsqueda")
            self._catalogue_group.add(row)
            self._catalogue_rows.append(row)
            return False

        for tk in toolkits:
            row = self._build_toolkit_row(tk)
            self._catalogue_group.add(row)
            self._catalogue_rows.append(row)

        return False

    def _build_toolkit_row(self, tk: dict) -> Gtk.Widget:
        row = Adw.ActionRow()
        row.set_title(tk.get("name", tk.get("slug", "")))

        desc = tk.get("description", "")
        if desc:
            row.set_subtitle(
                desc[:_DESC_MAX_LEN] + ("…" if len(desc) > _DESC_MAX_LEN else "")
            )

        connect_btn = Gtk.Button.new_with_label("Conectar")
        connect_btn.add_css_class("hermes-ghost")
        connect_btn.set_valign(Gtk.Align.CENTER)
        connect_btn.set_tooltip_text("Iniciar autenticación OAuth con esta app")
        slug = tk.get("slug", "")
        connect_btn.connect("clicked", lambda _b, s=slug: self._connect_app(s))
        row.add_suffix(connect_btn)

        return row

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _save_key(self) -> None:
        api_key = self._key_entry.get_text().strip()
        if not api_key:
            self._show_toast("Introduce tu API key de Composio")
            return

        self._save_key_btn.set_sensitive(False)
        self._save_key_btn.set_label("Guardando…")

        def runner() -> None:
            try:
                self._client.set_composio_key(api_key=api_key)
                GLib.idle_add(self._on_key_saved)
            except Exception as exc:  # noqa: BLE001
                msg = f"No se pudo guardar la key: {exc}"
                logger.warning("set_composio_key: %s", exc)
                GLib.idle_add(lambda m=msg: self._on_key_error(m))

        threading.Thread(target=runner, daemon=True, name="hermes-integrations-save-key").start()

    def _on_key_saved(self) -> bool:
        self._save_key_btn.set_sensitive(True)
        self._save_key_btn.set_label("Guardar key")
        self._key_entry.set_text("")
        self._show_toast("API key guardada — cargando catálogo…")
        self._reload()
        return False

    def _on_key_error(self, msg: str) -> bool:
        self._save_key_btn.set_sensitive(True)
        self._save_key_btn.set_label("Guardar key")
        self._show_toast(msg)
        return False

    def _connect_app(self, slug: str) -> None:
        def runner() -> None:
            try:
                result = self._client.composio_connect(slug=slug)
                redirect_url = result.get("redirect_url", "")
                if redirect_url:
                    GLib.idle_add(lambda url=redirect_url: self._open_url(url))
                else:
                    GLib.idle_add(
                        lambda: self._show_toast("No se recibió URL de autenticación")
                    )
            except Exception as exc:  # noqa: BLE001
                msg = f"Error al conectar {slug}: {exc}"
                logger.warning("composio_connect: %s", exc)
                GLib.idle_add(lambda m=msg: self._show_toast(m))

        threading.Thread(
            target=runner, daemon=True, name=f"hermes-integrations-connect-{slug}"
        ).start()

    def _disconnect(self, account_id: str) -> None:
        def runner() -> None:
            try:
                self._client.composio_disconnect(account_id=account_id)
                GLib.idle_add(self._load_connected)
                GLib.idle_add(
                    lambda: self._show_toast("App desconectada correctamente")
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"No se pudo desconectar: {exc}"
                logger.warning("composio_disconnect: %s", exc)
                GLib.idle_add(lambda m=msg: self._show_toast(m))

        threading.Thread(
            target=runner, daemon=True, name="hermes-integrations-disconnect"
        ).start()

    def _open_url(self, url: str) -> bool:
        """Open the OAuth redirect URL in the default system browser."""
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch_default_for_uri failed: %s", exc)
            self._show_toast(f"Abre en tu navegador: {url}")
        return False

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        q = entry.get_text().strip()
        # Only trigger catalogue search if the key is configured (page is visible).
        if self._content_stack.get_visible_child_name() == "has_key":
            self._load_catalogue(search=q)

    # ------------------------------------------------------------------
    # Toast helper
    # ------------------------------------------------------------------

    def _show_toast(self, msg: str) -> bool:
        toast = Adw.Toast.new(msg)
        toast.set_timeout(4)
        self._toast_overlay.add_toast(toast)
        return False
