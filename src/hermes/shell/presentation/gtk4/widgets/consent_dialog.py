"""HermesConsentDialog — modal cuando el agente pide capability.

UX:
  - Título visible: capability solicitada
  - Razón (text del agente)
  - Scope: Once | Session | Persistent
  - Botones: Conceder | Denegar
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from hermes.shell.infrastructure.shell_backend_client import (
    ShellBackendClient,
)

logger = logging.getLogger(__name__)


_CAPABILITY_LABELS = {
    "documents": "Documentos",
    "downloads": "Descargas",
    "desktop_files": "Escritorio",
    "filesystem_full": "Filesystem completo",
    "camera": "Cámara",
    "microphone": "Micrófono",
    "network_local": "Red local",
    "package_manager": "Instalador de paquetes",
    "system_settings": "Ajustes del sistema",
    "terminal": "Terminal",
}

_CAPABILITY_ICONS = {
    "documents": "folder-documents-symbolic",
    "downloads": "folder-download-symbolic",
    "desktop_files": "user-desktop-symbolic",
    "filesystem_full": "drive-harddisk-symbolic",
    "camera": "camera-web-symbolic",
    "microphone": "audio-input-microphone-symbolic",
    "network_local": "network-wired-symbolic",
    "package_manager": "system-software-install-symbolic",
    "system_settings": "preferences-system-symbolic",
    "terminal": "utilities-terminal-symbolic",
}


class HermesConsentDialog(Adw.Window):
    """Pide consent humano para una capability."""

    def __init__(
        self,
        *,
        parent: Gtk.Window,
        capability: str,
        requestor: str,
        reason: str | None = None,
        client: ShellBackendClient | None = None,
        on_decision: Callable[[bool, str], None] | None = None,
    ) -> None:
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(520, 400)
        self.set_title("Solicitud de consentimiento")
        self._capability = capability
        self._client = client or ShellBackendClient()
        self._on_decision = on_decision

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        cap_label = _CAPABILITY_LABELS.get(capability, capability)
        icon_name = _CAPABILITY_ICONS.get(capability, "dialog-question-symbolic")
        group.set_title(f"Hermes pide acceso a: {cap_label}")
        group.set_description(
            reason or f"El agente {requestor} solicita la capability '{capability}'."
        )

        # Status icon visible.
        status = Adw.StatusPage()
        status.set_icon_name(icon_name)
        status.set_title(cap_label)
        status.set_description(
            "Concédelo solo si reconoces lo que el agente intenta hacer.\n"
            "Puedes elegir el alcance: una vez, durante la sesión, o permanente."
        )

        # Scope selector.
        scope_row = Adw.ComboRow()
        scope_row.set_title("Alcance")
        scopes_model = Gtk.StringList.new(
            ["Una vez", "Durante la sesión", "Permanente"]
        )
        scope_row.set_model(scopes_model)
        scope_row.set_selected(1)
        self._scope_row = scope_row
        group.add(scope_row)

        page.add(group)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.append(status)
        body.append(page)

        toolbar.set_content(body)

        deny_btn = Gtk.Button.new_with_label("Denegar")
        deny_btn.connect("clicked", lambda _b: self._decide(False))
        header.pack_start(deny_btn)

        grant_btn = Gtk.Button.new_with_label("Conceder")
        grant_btn.add_css_class("hermes-primary")
        grant_btn.connect("clicked", lambda _b: self._decide(True))
        header.pack_end(grant_btn)

        self.set_content(toolbar)

    def _decide(self, granted: bool) -> None:
        scopes = ["once", "session", "persistent"]
        scope = scopes[self._scope_row.get_selected()] if granted else "once"
        if granted:
            def runner() -> None:
                try:
                    self._client.grant_consent(
                        capability=self._capability, scope=scope
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("grant consent: %s", exc)

            threading.Thread(target=runner, daemon=True).start()
        if self._on_decision is not None:
            self._on_decision(granted, scope)
        self.close()


def ask_consent(
    *,
    parent: Gtk.Window,
    capability: str,
    requestor: str = "agent",
    reason: str | None = None,
) -> None:
    """Convenience: abre el dialog sin esperar respuesta síncrona."""
    dlg = HermesConsentDialog(
        parent=parent,
        capability=capability,
        requestor=requestor,
        reason=reason,
    )
    dlg.present()
