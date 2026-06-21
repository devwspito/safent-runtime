"""HermesAuditView — Audit log firmado (hash-chain) del SO."""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from hermes.shell.infrastructure.shell_backend_client import (
    ShellBackendClient,
)

logger = logging.getLogger(__name__)


_KIND_ICONS = {
    "consent_granted": "emblem-ok-symbolic",
    "consent_revoked": "process-stop-symbolic",
    "ota_queued": "software-update-available-symbolic",
    "ota_promoted": "emblem-ok-symbolic",
    "ota_rejected": "dialog-warning-symbolic",
    "ota_rolled_back": "view-refresh-symbolic",
    "node_install_created": "computer-symbolic",
    "tenant_bound": "system-users-symbolic",
    "tenant_revoked": "process-stop-symbolic",
    "skill_promoted": "preferences-system-symbolic",
    "suspend_attempted": "media-playback-pause-symbolic",
    "suspend_denied": "process-stop-symbolic",
    "remote_control_issued": "network-wireless-symbolic",
    "remote_control_accepted": "network-wireless-symbolic",
    "remote_control_ended": "network-offline-symbolic",
    "landlock_ruleset_applied": "security-high-symbolic",
}


class HermesAuditView(Gtk.Box):
    def __init__(self, *, client: ShellBackendClient) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._client = client
        self.set_hexpand(True)
        self.set_vexpand(True)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.set_margin_top(12)
        toolbar.set_margin_bottom(8)
        toolbar.set_margin_start(24)
        toolbar.set_margin_end(24)

        title = Gtk.Label(label="Audit hash-chain")
        title.add_css_class("hermes-page-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        toolbar.append(title)

        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refrescar")
        refresh_btn.add_css_class("flat")
        refresh_btn.connect("clicked", lambda _b: self._reload())
        toolbar.append(refresh_btn)

        self.append(toolbar)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_start(24)
        self._list_box.set_margin_end(24)
        self._list_box.set_margin_bottom(24)

        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self._list_box)
        scroll.set_vexpand(True)
        self.append(scroll)

        self._reload()

    def _reload(self) -> None:
        # Vaciar.
        while (child := self._list_box.get_first_child()) is not None:
            self._list_box.remove(child)

        def runner() -> None:
            try:
                entries = self._client.list_audit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("audit list: %s", exc)
                entries = []
            GLib.idle_add(lambda: self._render(entries))

        threading.Thread(target=runner, daemon=True).start()

    def _render(self, entries: list[dict]) -> bool:
        if not entries:
            empty = Adw.StatusPage()
            empty.set_icon_name("document-properties-symbolic")
            empty.set_title("Audit vacío")
            empty.set_description(
                "El SO no ha producido aún ningún evento auditable.\n"
                "Cada acción se firmará con HMAC-SHA-256 hash chain."
            )
            self._list_box.append(empty)
            return False
        for e in entries:
            self._list_box.append(self._build_row(e))
        return False

    def _build_row(self, entry: dict) -> Gtk.Widget:
        row = Adw.ActionRow()
        row.set_title(entry.get("description", ""))
        ts = entry.get("timestamp", "")
        actor = entry.get("actor", "")
        kind = entry.get("audit_kind", "")
        sig = entry.get("signature_short", "")
        row.set_subtitle(f"{kind} · {actor} · {ts} · sig={sig}")
        icon_name = _KIND_ICONS.get(kind, "dialog-information-symbolic")
        icon = Gtk.Image.new_from_icon_name(icon_name)
        row.add_prefix(icon)
        return row
