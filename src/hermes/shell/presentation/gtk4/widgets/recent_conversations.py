"""RecentConversations — sección "Recientes" en la barra lateral.

Lista las conversaciones recientes (título + cuándo) obtenidas del backend
HTTP (supervisión read-only). Al pulsar una emite la señal `conversation-selected`
con el conversation_id.

Carga en hilo de background (threading.Thread) y publica en GTK main thread
via GLib.idle_add, igual que HermesChatHeader._load_conversations_into.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, GObject, Gtk, Pango  # noqa: E402

from hermes.shell.infrastructure.shell_backend_client import ShellBackendClient

logger = logging.getLogger(__name__)

_MAX_RECENTS = 8  # número máximo de conversaciones a mostrar


def _format_when(ts_str: str | None) -> str:
    """Convierte un ISO timestamp en texto legible relativo."""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        now = datetime.now(tz=timezone.utc)
        diff = now - dt
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "Ahora"
        if seconds < 3600:
            mins = seconds // 60
            return f"Hace {mins} min"
        if seconds < 86400:
            hours = seconds // 3600
            return f"Hace {hours} h"
        days = seconds // 86400
        if days == 1:
            return "Ayer"
        if days < 7:
            return f"Hace {days} días"
        return dt.strftime("%d/%m")
    except (ValueError, OSError):
        return ""


class RecentConversations(Gtk.Box):
    """Widget de la sección Recientes en el sidebar."""

    __gsignals__ = {
        "conversation-selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, *, client: ShellBackendClient) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._client = client

        # Título de sección.
        section_title = Gtk.Label(label="Recientes")
        section_title.set_xalign(0)
        section_title.add_css_class("hermes-sidebar-section-title")
        self.append(section_title)

        # Contenedor de items — se vacía y rellena en cada reload.
        self._items_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.append(self._items_box)

        # Estado de carga inicial.
        self._loading = True
        self._placeholder = Gtk.Label(label="Cargando…")
        self._placeholder.set_xalign(0)
        self._placeholder.add_css_class("hermes-recent-placeholder")
        self._items_box.append(self._placeholder)

        self._reload()

    # ------------------------------------------------------------------
    # Reload (llamado también desde window.py tras new-conversation)
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        def runner() -> None:
            try:
                convs = self._client.list_conversations()
            except Exception as exc:  # noqa: BLE001
                logger.warning("recent conversations: %s", exc)
                convs = []
            GLib.idle_add(self._render, convs)

        threading.Thread(target=runner, daemon=True).start()

    def _render(self, convs: list[dict]) -> bool:
        # Limpiar contenido anterior.
        while (child := self._items_box.get_first_child()) is not None:
            self._items_box.remove(child)

        if not convs:
            empty = Gtk.Label(label="Sin conversaciones aún")
            empty.set_xalign(0)
            empty.add_css_class("hermes-recent-placeholder")
            self._items_box.append(empty)
            return False

        for conv in convs[:_MAX_RECENTS]:
            self._items_box.append(self._build_item(conv))

        return False

    def _build_item(self, conv: dict) -> Gtk.Widget:
        conv_id = conv.get("conversation_id", "")
        title = conv.get("title") or "Conversación"
        when = _format_when(conv.get("last_msg_at"))

        btn = Gtk.Button()
        btn.add_css_class("hermes-recent-item")
        btn.set_has_frame(False)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        inner.set_margin_top(6)
        inner.set_margin_bottom(6)

        title_lbl = Gtk.Label(label=title)
        title_lbl.set_xalign(0)
        title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_lbl.add_css_class("hermes-recent-title")
        inner.append(title_lbl)

        if when:
            when_lbl = Gtk.Label(label=when)
            when_lbl.set_xalign(0)
            when_lbl.add_css_class("hermes-recent-when")
            inner.append(when_lbl)

        btn.set_child(inner)
        btn.connect("clicked", lambda _b, cid=conv_id: self.emit("conversation-selected", cid))
        return btn

    # ------------------------------------------------------------------
    # Public: called from window.py after new conversation / agent change
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Trigger an async reload of the recent conversations list."""
        self._reload()
