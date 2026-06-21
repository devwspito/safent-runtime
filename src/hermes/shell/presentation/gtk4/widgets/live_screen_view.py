"""HermesLiveScreenView — pantalla en vivo del compositor (F7.1).

Muestra EN VIVO lo que mutter compone: el navegador del agente Y cualquier
app de escritorio (no solo web). Captura unificada via ScreenCaptureService
(mutter ScreenCast → PipeWire → GStreamer appsink RGBA) y render en un
Gtk.Picture con Gdk.MemoryTexture.

Vive en el proceso hermes-shell (GTK4), que está dentro de la sesión mutter
y por tanto puede hablar con org.gnome.Mutter.ScreenCast.
"""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from hermes.shell_server.screen_capture.domain import CaptureTarget, Frame
from hermes.shell_server.screen_capture.service import ScreenCaptureService

logger = logging.getLogger(__name__)


class HermesLiveScreenView(Gtk.Box):
    """Tab 'Pantalla en vivo': captura + render del compositor entero."""

    def __init__(self, *, service: ScreenCaptureService | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("hermes-live-screen")
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._service = service or ScreenCaptureService()
        self._capturing = False

        # --- toolbar ---
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(8)
        bar.set_margin_bottom(8)
        bar.set_margin_start(8)
        bar.set_margin_end(8)

        self._toggle = Gtk.Button.new_with_label("Ver pantalla en vivo")
        self._toggle.add_css_class("hermes-primary")
        self._toggle.connect("clicked", lambda _b: self._on_toggle())
        bar.append(self._toggle)

        self._status = Gtk.Label(label="Detenido")
        self._status.add_css_class("dim-label")
        self._status.set_hexpand(True)
        self._status.set_xalign(0.0)
        self._status.set_margin_start(8)
        bar.append(self._status)

        self.append(bar)

        # --- video surface ---
        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.add_css_class("hermes-live-surface")

        self._empty = Adw.StatusPage()
        self._empty.set_icon_name("video-display-symbolic")
        self._empty.set_title("Pantalla en vivo")
        self._empty.set_description(
            "Ver en directo lo que hace el agente: navegador y apps de\n"
            "escritorio. Pulsa «Ver pantalla en vivo» para empezar."
        )

        self._frame_seen = False
        self.append(self._empty)
        self._video_attached = False

    # ------------------------------------------------------------------
    def _on_toggle(self) -> None:
        if self._capturing:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        self._toggle.set_sensitive(False)
        self._status.set_text("Iniciando captura…")

        def runner() -> None:
            try:
                self._service.subscribe(self._on_frame)
                self._service.start(CaptureTarget.monitor(self._connector()))
            except Exception as exc:  # noqa: BLE001
                logger.warning("live screen start failed: %s", exc)
                GLib.idle_add(self._show_error, str(exc))
                return
            GLib.idle_add(self._mark_capturing)

        threading.Thread(target=runner, daemon=True).start()

    def _connector(self) -> str:
        # None → el source resuelve el monitor primario. Devolvemos "" para
        # que MutterScreenCastSource use primary_connector() internamente.
        return ""

    def _mark_capturing(self) -> bool:
        self._capturing = True
        self._toggle.set_label("Detener")
        self._toggle.set_sensitive(True)
        self._status.set_text("Capturando…")
        return False

    def _show_error(self, msg: str) -> bool:
        self._toggle.set_sensitive(True)
        self._toggle.set_label("Reintentar")
        self._status.set_text(f"Error: {msg}")
        return False

    def _on_frame(self, frame: Frame) -> None:
        # Llega en el thread de GStreamer → saltar al main loop para tocar GTK.
        GLib.idle_add(self._render_frame, frame)

    def _render_frame(self, frame: Frame) -> bool:
        if frame.is_blank():
            return False
        gbytes = GLib.Bytes.new(frame.data)
        texture = Gdk.MemoryTexture.new(
            frame.width,
            frame.height,
            Gdk.MemoryFormat.R8G8B8A8,
            gbytes,
            frame.stride,
        )
        if not self._video_attached:
            self.remove(self._empty)
            self.append(self._picture)
            self._video_attached = True
        self._picture.set_paintable(texture)
        if not self._frame_seen:
            self._frame_seen = True
            self._status.set_text(f"En vivo · {frame.width}×{frame.height}")
        return False

    def _stop(self) -> None:
        # Disable toggle immediately to prevent re-entrant clicks.
        self._toggle.set_sensitive(False)
        self._status.set_text("Deteniendo…")

        # Optimistically clear capture flag so no new frames are rendered.
        self._capturing = False

        def _teardown() -> None:
            try:
                self._service.unsubscribe(self._on_frame)
                self._service.stop()
            except Exception:  # noqa: BLE001
                logger.exception("live screen stop failed")
            GLib.idle_add(self._mark_stopped)

        threading.Thread(target=_teardown, daemon=True).start()

    def _mark_stopped(self) -> bool:
        self._frame_seen = False
        self._toggle.set_label("Ver pantalla en vivo")
        self._toggle.set_sensitive(True)
        self._status.set_text("Detenido")
        if self._video_attached:
            self.remove(self._picture)
            self.append(self._empty)
            self._video_attached = False
        return False
