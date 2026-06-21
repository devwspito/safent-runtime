"""HermesRemoteAccessView — remote-access tunnel control panel.

Shows the tunnel URL + a state-aware enable/disable control.

Disable flow (consent-gated):
  1. User clicks "Desactivar acceso remoto".
  2. AdwAlertDialog (warning + PasswordEntry).
  3. On confirm → POST /api/v1/remote-access/disable with the password.
  4. 403/429 → inline error, dialog stays open, entry cleared.
  5. 200 → dialog closed, toast "Desactivado", status polled.

Enable flow (free):
  1. User clicks "Activar acceso remoto".
  2. Immediate POST /api/v1/remote-access/enable.
  3. Toast + status poll.

Threading contract:
  - HTTP calls are made in a background thread (not GTK thread).
  - All GTK mutations happen via GLib.idle_add.
  - The URL file read (< 1 ms, no network) stays on the GTK thread.

Override env vars for tests:
  HERMES_REMOTE_URL_FILE  — URL file path override.
  HERMES_SHELL_BACKEND_URL — backend URL override (picked up by client).
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.error
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

logger = logging.getLogger(__name__)

_DEFAULT_URL_FILE = Path("/var/lib/hermes/remote-url")

# Poll interval after a toggle action to confirm state change.
_STATUS_POLL_DELAY_MS = 2000
_STATUS_POLL_RETRIES = 6


# ---------------------------------------------------------------------------
# Pure helpers — no GTK dependency; importable headlessly.
# ---------------------------------------------------------------------------


def _read_remote_url(path: Path) -> str | None:
    """Read and return the tunnel URL from *path*.

    Returns:
        The stripped URL string when the file exists and is non-empty.
        ``None`` when the file is missing, unreadable, or blank.

    Never raises.  Never logs the URL (it contains a VNC password).
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("remote-url: cannot read %s: %s", path, exc)
        return None
    return text if text else None


def _url_file_path() -> Path:
    override = os.environ.get("HERMES_REMOTE_URL_FILE")
    return Path(override) if override else _DEFAULT_URL_FILE


def _parse_active(status_dict: dict) -> bool:
    """Extract the 'active' bool from a status response dict."""
    return bool(status_dict.get("active", False))


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class HermesRemoteAccessView(Gtk.Box):
    """Vista de acceso remoto — muestra el enlace del túnel y permite copiarlo
    o desactivar/activar el acceso remoto con consentimiento."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_vexpand(True)
        self._toast_overlay.set_hexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_hexpand(True)
        inner.set_vexpand(True)
        inner.append(self._build_toolbar())
        inner.append(self._build_content())

        self._toast_overlay.set_child(inner)
        self.append(self._toast_overlay)

        # Kick off async state load.
        self._reload()
        self._refresh_status()

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------

    def _build_toolbar(self) -> Gtk.Widget:
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.add_css_class("hermes-view-toolbar")

        title = Gtk.Label(label="Acceso remoto")
        title.add_css_class("hermes-page-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        toolbar.append(title)

        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text("Actualizar enlace")
        refresh_btn.connect("clicked", lambda _b: self._reload())
        toolbar.append(refresh_btn)

        return toolbar

    # ------------------------------------------------------------------
    # Content area
    # ------------------------------------------------------------------

    def _build_content(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(680)
        clamp.set_margin_top(32)
        clamp.set_margin_bottom(32)
        clamp.set_margin_start(24)
        clamp.set_margin_end(24)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)

        self._content_stack = Gtk.Stack()
        self._content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._content_stack.set_transition_duration(160)

        self._content_stack.add_named(self._build_url_page(), "url")
        self._content_stack.add_named(self._build_waiting_page(), "waiting")
        self._content_stack.add_named(self._build_error_page(), "error")

        outer.append(self._content_stack)
        outer.append(self._build_control_section())
        clamp.set_child(outer)
        scroll.set_child(clamp)
        return scroll

    def _build_url_page(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup()
        group.set_title("Enlace de conexión")
        group.set_description(
            "Abre este enlace en cualquier dispositivo y navegador para controlar "
            "tu Hermes en remoto (teclado, ratón y portapapeles). "
            "La contraseña ya va incluida en el enlace."
        )

        url_row = Adw.ActionRow()
        url_row.set_title("Enlace")

        self._url_label = Gtk.Label()
        self._url_label.set_selectable(True)
        self._url_label.set_wrap(True)
        self._url_label.set_xalign(0)
        self._url_label.set_valign(Gtk.Align.CENTER)
        self._url_label.set_hexpand(True)
        self._url_label.add_css_class("hermes-remote-url-text")
        url_row.set_child(self._url_label)

        group.add(url_row)

        copy_btn = Gtk.Button.new_with_label("Copiar enlace")
        copy_btn.add_css_class("hermes-primary")
        copy_btn.set_halign(Gtk.Align.START)
        copy_btn.set_tooltip_text("Copiar enlace al portapapeles")
        copy_btn.connect("clicked", self._on_copy_clicked)
        self._copy_btn = copy_btn

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        page.append(group)
        page.append(copy_btn)
        return page

    def _build_waiting_page(self) -> Gtk.Widget:
        status = Adw.StatusPage()
        status.set_icon_name("network-wireless-symbolic")
        status.set_title("Generando enlace…")
        status.set_description(
            "Generando enlace de acceso remoto…\n"
            "Espera unos segundos y pulsa Actualizar."
        )
        status.set_vexpand(True)

        retry_btn = Gtk.Button.new_with_label("Actualizar")
        retry_btn.add_css_class("hermes-ghost")
        retry_btn.set_halign(Gtk.Align.CENTER)
        retry_btn.connect("clicked", lambda _b: self._reload())
        status.set_child(retry_btn)

        return status

    def _build_error_page(self) -> Gtk.Widget:
        self._error_status = Adw.StatusPage()
        self._error_status.set_icon_name("dialog-warning-symbolic")
        self._error_status.set_title("No se pudo leer el enlace")
        self._error_status.set_description(
            "Comprueba que el servicio hermes-remote-tunnel está activo\n"
            "y vuelve a intentarlo."
        )
        self._error_status.set_vexpand(True)

        retry_btn = Gtk.Button.new_with_label("Actualizar")
        retry_btn.add_css_class("hermes-ghost")
        retry_btn.set_halign(Gtk.Align.CENTER)
        retry_btn.connect("clicked", lambda _b: self._reload())
        self._error_status.set_child(retry_btn)

        return self._error_status

    # ------------------------------------------------------------------
    # Control section — enable / disable tunnel
    # ------------------------------------------------------------------

    def _build_control_section(self) -> Gtk.Widget:
        """Build the enable/disable toggle section."""
        self._control_group = Adw.PreferencesGroup()
        self._control_group.set_title("Control de acceso remoto")

        control_row = Adw.ActionRow()
        control_row.set_title("Estado del túnel")
        self._status_label = Gtk.Label(label="Comprobando…")
        self._status_label.set_valign(Gtk.Align.CENTER)
        self._status_label.add_css_class("dim-label")
        control_row.add_suffix(self._status_label)
        self._control_group.add(control_row)

        # The toggle button — label and style are updated by _apply_active_state.
        self._toggle_btn = Gtk.Button()
        self._toggle_btn.set_halign(Gtk.Align.START)
        self._toggle_btn.connect("clicked", self._on_toggle_clicked)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(self._control_group)
        box.append(self._toggle_btn)
        return box

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def _apply_active_state(self, active: bool) -> None:
        """Update the control section to reflect *active* state.

        Must be called on the GTK main thread.
        """
        if active:
            self._status_label.set_text("Activo")
            self._toggle_btn.set_label("Desactivar acceso remoto")
            self._toggle_btn.remove_css_class("suggested-action")
            self._toggle_btn.add_css_class("destructive-action")
        else:
            self._status_label.set_text("Inactivo")
            self._toggle_btn.set_label("Activar acceso remoto")
            self._toggle_btn.remove_css_class("destructive-action")
            self._toggle_btn.add_css_class("suggested-action")

    # ------------------------------------------------------------------
    # Reload — URL file
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        path = _url_file_path()
        url = _read_remote_url(path)

        if url is None:
            if path.exists():
                self._content_stack.set_visible_child_name("error")
            else:
                self._content_stack.set_visible_child_name("waiting")
            return

        self._url_label.set_text(url)
        self._content_stack.set_visible_child_name("url")

    # ------------------------------------------------------------------
    # Status refresh (background thread → GTK thread)
    # ------------------------------------------------------------------

    def _refresh_status(self) -> None:
        """Fetch service status in a background thread; update UI on result."""

        def _fetch() -> None:
            from hermes.shell.infrastructure.shell_backend_client import (  # noqa: PLC0415
                ShellBackendClient,
            )

            client = ShellBackendClient()
            try:
                result = client.remote_access_status()
                active = _parse_active(result)
                GLib.idle_add(self._apply_active_state, active)
            except Exception as exc:  # noqa: BLE001
                logger.debug("remote_access_status failed: %s", exc)
                GLib.idle_add(self._status_label.set_text, "Desconocido")

        threading.Thread(target=_fetch, daemon=True, name="hermes-ra-status").start()

    # ------------------------------------------------------------------
    # Toggle button
    # ------------------------------------------------------------------

    def _on_toggle_clicked(self, _btn: Gtk.Button) -> None:
        label = self._toggle_btn.get_label()
        if "Desactivar" in label:
            self._show_disable_dialog()
        else:
            self._do_enable()

    # ------------------------------------------------------------------
    # Enable flow
    # ------------------------------------------------------------------

    def _do_enable(self) -> None:
        self._toggle_btn.set_sensitive(False)

        def _call() -> None:
            from hermes.shell.infrastructure.shell_backend_client import (  # noqa: PLC0415
                ShellBackendClient,
            )

            client = ShellBackendClient()
            try:
                client.remote_access_enable()
                GLib.idle_add(self._on_enable_success)
            except Exception as exc:  # noqa: BLE001
                logger.warning("remote_access_enable failed: %s", exc)
                GLib.idle_add(self._on_action_error, "No se pudo activar el acceso remoto.")

        threading.Thread(target=_call, daemon=True, name="hermes-ra-enable").start()

    def _on_enable_success(self) -> bool:
        self._toggle_btn.set_sensitive(True)
        self._show_toast("Activando acceso remoto…")
        self._schedule_status_poll(expected_active=True)
        return False

    # ------------------------------------------------------------------
    # Disable flow
    # ------------------------------------------------------------------

    def _show_disable_dialog(self) -> None:
        """Show the consent dialog: warning + password entry."""
        dialog = Adw.AlertDialog()
        dialog.set_heading("Desactivar acceso remoto")
        dialog.set_body(
            "Si no estás físicamente delante de este equipo podrías perder el "
            "control de forma permanente.\n\n"
            "Introduce la contraseña del dispositivo para confirmar."
        )

        # Password entry inside the dialog.
        password_entry = Gtk.PasswordEntry()
        password_entry.set_show_peek_icon(True)
        password_entry.set_placeholder_text("Contraseña del dispositivo")
        password_entry.set_margin_top(12)

        # Error label — hidden by default.
        error_label = Gtk.Label()
        error_label.add_css_class("error")
        error_label.set_xalign(0)
        error_label.set_visible(False)
        error_label.set_margin_top(4)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content_box.append(password_entry)
        content_box.append(error_label)
        dialog.set_extra_child(content_box)

        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("confirm", "Desactivar")
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(d: Adw.AlertDialog, response: str) -> None:
            if response != "confirm":
                return
            password = password_entry.get_text()
            # Clear the entry immediately — password must not linger in memory.
            password_entry.set_text("")
            self._do_disable(
                password=password,
                dialog=d,
                error_label=error_label,
                password_entry=password_entry,
            )

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

    def _do_disable(
        self,
        *,
        password: str,
        dialog: Adw.AlertDialog,
        error_label: Gtk.Label,
        password_entry: Gtk.PasswordEntry,
    ) -> None:
        """Call the disable endpoint in a background thread."""

        def _call() -> None:
            from hermes.shell.infrastructure.shell_backend_client import (  # noqa: PLC0415
                ShellBackendClient,
            )
            import urllib.error as _ue  # noqa: PLC0415

            client = ShellBackendClient()
            try:
                client.remote_access_disable(password=password)
                GLib.idle_add(_on_success)
            except _ue.HTTPError as exc:
                if exc.code in (403, 400):
                    GLib.idle_add(_on_wrong_password)
                elif exc.code == 429:
                    GLib.idle_add(_on_rate_limited)
                else:
                    GLib.idle_add(_on_generic_error, f"Error {exc.code}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("remote_access_disable failed: %s", exc)
                GLib.idle_add(_on_generic_error, "Error de conexión.")

        def _on_success() -> bool:
            dialog.close()
            self._show_toast("Desactivando acceso remoto…")
            self._schedule_status_poll(expected_active=False)
            return False

        def _on_wrong_password() -> bool:
            error_label.set_text("Contraseña incorrecta.")
            error_label.set_visible(True)
            password_entry.add_css_class("error")
            return False

        def _on_rate_limited() -> bool:
            error_label.set_text("Demasiados intentos. Espera un momento.")
            error_label.set_visible(True)
            return False

        def _on_generic_error(msg: str) -> bool:
            error_label.set_text(msg)
            error_label.set_visible(True)
            return False

        threading.Thread(target=_call, daemon=True, name="hermes-ra-disable").start()

    # ------------------------------------------------------------------
    # Status polling after toggle action
    # ------------------------------------------------------------------

    def _schedule_status_poll(self, *, expected_active: bool, attempt: int = 0) -> None:
        """Poll service status until it matches *expected_active* or retries exhaust."""

        def _poll() -> bool:
            if attempt >= _STATUS_POLL_RETRIES:
                self._refresh_status()
                return False
            self._poll_once(expected_active=expected_active, attempt=attempt)
            return False

        GLib.timeout_add(_STATUS_POLL_DELAY_MS, _poll)

    def _poll_once(self, *, expected_active: bool, attempt: int) -> None:
        def _fetch() -> None:
            from hermes.shell.infrastructure.shell_backend_client import (  # noqa: PLC0415
                ShellBackendClient,
            )

            client = ShellBackendClient()
            try:
                result = client.remote_access_status()
                active = _parse_active(result)
            except Exception:  # noqa: BLE001
                active = expected_active  # don't block on network error

            def _update() -> bool:
                self._apply_active_state(active)
                if active != expected_active:
                    # State not yet changed — try again.
                    self._schedule_status_poll(
                        expected_active=expected_active, attempt=attempt + 1
                    )
                return False

            GLib.idle_add(_update)

        threading.Thread(
            target=_fetch, daemon=True, name="hermes-ra-poll"
        ).start()

    # ------------------------------------------------------------------
    # Error helper (for enable/non-dialog errors)
    # ------------------------------------------------------------------

    def _on_action_error(self, msg: str) -> bool:
        self._toggle_btn.set_sensitive(True)
        self._show_toast(msg)
        return False

    # ------------------------------------------------------------------
    # Copy action
    # ------------------------------------------------------------------

    def _on_copy_clicked(self, _btn: Gtk.Button) -> None:
        url = self._url_label.get_text()
        if not url:
            return

        display = Gdk.Display.get_default()
        if display is None:
            return

        clipboard = display.get_clipboard()
        clipboard.set(url)

        GLib.idle_add(self._show_toast, "Enlace copiado al portapapeles")

    # ------------------------------------------------------------------
    # Toast helper
    # ------------------------------------------------------------------

    def _show_toast(self, msg: str) -> bool:
        toast = Adw.Toast.new(msg)
        toast.set_timeout(3)
        self._toast_overlay.add_toast(toast)
        return False
