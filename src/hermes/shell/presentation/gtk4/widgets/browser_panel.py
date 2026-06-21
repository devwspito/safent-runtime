"""HermesBrowserPanel — pestaña Navegador del workspace.

Permite al humano arrancar/parar el Chromium del agente y muestra
estado + URL CDP.
"""

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


class HermesBrowserPanel(Gtk.Box):
    """Panel con botón Start/Stop + status del Chromium."""

    def __init__(self, *, client: ShellBackendClient) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._client = client
        self.set_vexpand(True)
        self.set_hexpand(True)

        self._status_page = Adw.StatusPage()
        self._status_page.set_icon_name("web-browser-symbolic")
        self._status_page.set_title("Navegador del agente")
        self._status_page.set_vexpand(True)
        self._update_status_view("stopped", None, None)

        controls_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        controls_box.set_halign(Gtk.Align.CENTER)
        controls_box.set_margin_top(16)
        controls_box.set_margin_bottom(16)

        self._start_btn = Gtk.Button.new_with_label("Arrancar Chromium")
        self._start_btn.add_css_class("hermes-primary")
        self._start_btn.connect("clicked", lambda _b: self._start())
        controls_box.append(self._start_btn)

        self._stop_btn = Gtk.Button.new_with_label("Detener")
        self._stop_btn.add_css_class("hermes-ghost")
        self._stop_btn.connect("clicked", lambda _b: self._stop())
        self._stop_btn.set_sensitive(False)
        controls_box.append(self._stop_btn)

        self._refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self._refresh_btn.set_tooltip_text("Refrescar estado")
        self._refresh_btn.add_css_class("flat")
        self._refresh_btn.connect("clicked", lambda _b: self._refresh())
        controls_box.append(self._refresh_btn)

        self.append(self._status_page)
        self.append(controls_box)

        self._refresh()

    def _async(self, fn) -> None:
        def runner() -> None:
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001
                logger.warning("browser action error: %s", exc)
                result = {"state": "error", "error": str(exc)}
            GLib.idle_add(lambda: self._on_action_done(result))

        threading.Thread(target=runner, daemon=True).start()

    def _on_action_done(self, result: dict) -> bool:
        if isinstance(result, dict) and result.get("state") == "error":
            self._status_page.set_description(f"Error: {result.get('error')}")
            return False
        state = result.get("state", "unknown") if isinstance(result, dict) else "unknown"
        pid = result.get("pid") if isinstance(result, dict) else None
        cdp_url = result.get("cdp_url") if isinstance(result, dict) else None
        self._update_status_view(state, pid, cdp_url)
        return False

    def _start(self) -> None:
        self._async(lambda: self._client._request(  # type: ignore[attr-defined]
            path="/api/v1/browser/start", method="POST"
        ))

    def _stop(self) -> None:
        self._async(lambda: self._client._request(  # type: ignore[attr-defined]
            path="/api/v1/browser/stop", method="POST"
        ))

    def _refresh(self) -> None:
        self._async(lambda: self._client._request(  # type: ignore[attr-defined]
            path="/api/v1/browser"
        ))

    def _update_status_view(
        self, state: str, pid: int | None, cdp_url: str | None
    ) -> None:
        labels = {
            "stopped": ("Chromium detenido", "Arranca el navegador del agente para que pueda operar sitios web."),
            "running": (
                "Chromium en marcha",
                f"pid={pid} · CDP={cdp_url or '?'}",
            ),
            "starting": ("Arrancando…", ""),
        }
        title, desc = labels.get(state, ("Estado desconocido", state))
        self._status_page.set_title(title)
        self._status_page.set_description(desc)
        running = state == "running"
        self._start_btn.set_sensitive(not running)
        self._stop_btn.set_sensitive(running)
