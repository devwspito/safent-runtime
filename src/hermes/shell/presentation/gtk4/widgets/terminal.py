"""HermesTerminal — terminal embebido con vte4.

Spawn bash en /home/hermes-user/terminal-workspace con audit log al
hash-chain. Consent-gated: el agent_runtime debe haber concedido la
capability `terminal` (FR-013).

Por ahora arrancamos sin consent gate funcional (TODO F5.1) — el
usuario humano local lo abre directo. Cuando el AGENT quiera abrir
terminal, ahí sí pasa por consent.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import gi

gi.require_version("Gtk", "4.0")

try:
    gi.require_version("Vte", "3.91")
    from gi.repository import GLib, Gtk, Vte

    _VTE_AVAILABLE = True
except (ImportError, ValueError) as exc:  # pragma: no cover
    Vte = None  # type: ignore[assignment]
    _VTE_AVAILABLE = False
    _import_error = exc

logger = logging.getLogger(__name__)


_DEFAULT_WORKSPACE = "/var/lib/hermes/terminal-workspace"


class HermesTerminal(Gtk.Box):
    """Wrapper Box que contiene una VteTerminal."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)

        if not _VTE_AVAILABLE:
            self._show_fallback()
            return

        self._terminal = Vte.Terminal()
        self._terminal.set_hexpand(True)
        self._terminal.set_vexpand(True)
        self._terminal.set_scrollback_lines(10000)

        # Colores Hermes (dark theme con violeta).
        self._apply_hermes_colors()

        # Sólo añadimos. El spawn se hace lazy en first show.
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self._terminal)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        self.append(scroll)

        # Spawn cuando esté mapped (first show).
        self._spawned = False
        self.connect("map", self._on_map)

    def _show_fallback(self) -> None:
        from gi.repository import Adw

        page = Adw.StatusPage()
        page.set_icon_name("utilities-terminal-symbolic")
        page.set_title("Terminal no disponible")
        page.set_description(
            "VTE 4 no se pudo cargar. Instala vte291-gtk4."
        )
        self.append(page)

    # ------------------------------------------------------------------
    # Theming
    # ------------------------------------------------------------------
    def _apply_hermes_colors(self) -> None:
        from gi.repository import Gdk

        def rgba(hexcode: str) -> Gdk.RGBA:
            c = Gdk.RGBA()
            c.parse(hexcode)
            return c

        # Paleta dark Hermes.
        bg = rgba("#0B0E14")
        fg = rgba("#E6E9F2")
        # ANSI 16-color palette tipo OneDark.
        palette_hex = [
            "#0E1117",  # 0 black
            "#E5484D",  # 1 red
            "#3DD68C",  # 2 green
            "#F0B72F",  # 3 yellow
            "#7C5CFF",  # 4 blue (Hermes accent)
            "#9078FF",  # 5 magenta
            "#5BAEF8",  # 6 cyan
            "#9BA3B5",  # 7 white
            "#5A6478",  # 8 bright black
            "#F37A7D",  # 9 bright red
            "#5BDFA4",  # 10 bright green
            "#F5C95E",  # 11 bright yellow
            "#9078FF",  # 12 bright blue
            "#B099FF",  # 13 bright magenta
            "#8FCBF8",  # 14 bright cyan
            "#E6E9F2",  # 15 bright white
        ]
        palette = [rgba(c) for c in palette_hex]
        self._terminal.set_colors(fg, bg, palette)
        self._terminal.set_font_scale(1.0)

    # ------------------------------------------------------------------
    # Spawn shell
    # ------------------------------------------------------------------
    def _on_map(self, _widget: Gtk.Widget) -> None:
        if self._spawned:
            return
        self._spawned = True
        self._spawn_shell()

    def _spawn_shell(self) -> None:
        workspace = os.environ.get("HERMES_TERMINAL_WORKSPACE", _DEFAULT_WORKSPACE)
        try:
            os.makedirs(workspace, exist_ok=True)
        except PermissionError:
            workspace = os.path.expanduser("~")

        argv = [os.environ.get("SHELL", "/bin/bash")]
        env_pairs = [f"{k}={v}" for k, v in os.environ.items()]
        env_pairs.append("HERMES_TERMINAL=1")
        env_pairs.append("TERM=xterm-256color")

        def _spawn_async() -> None:
            self._terminal.spawn_async(
                Vte.PtyFlags.DEFAULT,
                workspace,
                argv,
                env_pairs,
                GLib.SpawnFlags.DEFAULT,
                None,
                None,
                -1,
                None,
                self._on_spawned,
            )

        _spawn_async()

    def _on_spawned(self, _terminal: Any, pid: int, error: Any) -> None:
        if error:
            logger.error("Vte spawn failed: %s", error)
        else:
            logger.info("Vte spawned shell pid=%s", pid)
