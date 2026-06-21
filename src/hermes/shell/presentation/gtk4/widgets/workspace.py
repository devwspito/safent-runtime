"""HermesWorkspace — panel derecho con tabs (browser, terminal, files, training)."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from hermes.shell.infrastructure.shell_backend_client import (
    ShellBackendClient,
)

logger = logging.getLogger(__name__)


class HermesWorkspace(Gtk.Box):
    """Workspace con tabs: browser embebido, terminal, files, recording."""

    def __init__(self, *, client: ShellBackendClient | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("hermes-workspace-pane")
        self.set_hexpand(True)
        self._client = client or ShellBackendClient()

        self._stack = Adw.ViewStack()
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)

        self._stack.add_titled_with_icon(
            self._build_live_screen_panel(),
            "live",
            "Pantalla",
            "video-display-symbolic",
        )
        self._stack.add_titled_with_icon(
            self._build_browser_panel(),
            "browser",
            "Navegador",
            "web-browser-symbolic",
        )
        self._stack.add_titled_with_icon(
            self._build_terminal_panel(),
            "terminal",
            "Terminal",
            "utilities-terminal-symbolic",
        )
        self._stack.add_titled_with_icon(
            self._build_files_panel(),
            "files",
            "Archivos",
            "folder-symbolic",
        )
        self._stack.add_titled_with_icon(
            self._build_training_panel(),
            "recording",
            "Grabación",
            "media-record-symbolic",
        )

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self._stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        switcher.add_css_class("hermes-workspace-tabs")

        switcher_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        switcher_box.add_css_class("hermes-workspace-tabs")
        switcher_box.append(switcher)
        self.append(switcher_box)
        self.append(self._stack)

    def _build_live_screen_panel(self) -> Gtk.Widget:
        try:
            from hermes.shell.presentation.gtk4.widgets.live_screen_view import (
                HermesLiveScreenView,
            )

            return HermesLiveScreenView()
        except Exception as exc:  # noqa: BLE001
            return _fallback_status(
                "video-display-symbolic", "Pantalla en vivo", str(exc)
            )

    def _build_browser_panel(self) -> Gtk.Widget:
        try:
            from hermes.shell.presentation.gtk4.widgets.browser_panel import (
                HermesBrowserPanel,
            )

            return HermesBrowserPanel(client=self._client)
        except Exception as exc:  # noqa: BLE001
            return _fallback_status("web-browser-symbolic", "Navegador", str(exc))

    def _build_terminal_panel(self) -> Gtk.Widget:
        try:
            from hermes.shell.presentation.gtk4.widgets.terminal import (
                HermesTerminal,
            )

            return HermesTerminal()
        except Exception as exc:  # noqa: BLE001
            return _fallback_status(
                "utilities-terminal-symbolic", "Terminal", str(exc)
            )

    def _build_files_panel(self) -> Gtk.Widget:
        return _fallback_status(
            "folder-symbolic",
            "Archivos",
            "Acceso a /home/hermes-user/{Documents,Downloads,Desktop}\n"
            "con capability consent por carpeta. F8.",
        )

    def _build_training_panel(self) -> Gtk.Widget:
        try:
            from hermes.shell.presentation.gtk4.widgets.training_panel import (
                HermesTrainingPanel,
            )

            return HermesTrainingPanel(client=self._client)
        except Exception as exc:  # noqa: BLE001
            return _fallback_status(
                "media-record-symbolic", "Grabación (training)", str(exc)
            )


def _fallback_status(icon: str, title: str, desc: str) -> Gtk.Widget:
    page = Adw.StatusPage()
    page.set_icon_name(icon)
    page.set_title(title)
    page.set_description(desc)
    return page
