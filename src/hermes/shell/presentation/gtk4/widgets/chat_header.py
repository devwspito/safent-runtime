"""HermesChatHeader — barra superior del chat con nuevo + historial."""

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


class HermesChatHeader(Gtk.Box):
    """Barra con título de conversation + acciones nuevo/historial/borrar."""

    def __init__(
        self,
        *,
        client: ShellBackendClient,
        on_new_chat: Callable[[], None],
        on_open_conv: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._client = client
        self._on_new_chat = on_new_chat
        self._on_open_conv = on_open_conv
        self.add_css_class("hermes-chat-header")
        self.set_margin_top(8)
        self.set_margin_bottom(8)
        self.set_margin_start(16)
        self.set_margin_end(16)

        self._title = Gtk.Label()
        self._title.set_xalign(0)
        self._title.set_hexpand(True)
        self._title.add_css_class("hermes-chat-title")
        self.set_title("Hermes")
        self.append(self._title)

        history_btn = Gtk.Button.new_from_icon_name("document-open-recent-symbolic")
        history_btn.set_tooltip_text("Conversaciones anteriores")
        history_btn.add_css_class("flat")
        history_btn.connect("clicked", self._on_history_clicked)
        self.append(history_btn)

        new_btn = Gtk.Button.new_from_icon_name("document-new-symbolic")
        new_btn.set_tooltip_text("Nueva conversación")
        new_btn.add_css_class("flat")
        new_btn.connect("clicked", lambda _b: self._on_new_chat())
        self.append(new_btn)

        self._popover: Gtk.Popover | None = None

    def set_title(self, text: str) -> None:
        self._title.set_text(text or "Hermes")

    def _on_history_clicked(self, button: Gtk.Button) -> None:
        # Crear popover bajo el botón.
        popover = Gtk.Popover()
        popover.set_parent(button)
        popover.set_has_arrow(False)
        popover.set_size_request(360, 460)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        title = Gtk.Label(label="Conversaciones")
        title.add_css_class("hermes-sidebar-section-title")
        title.set_xalign(0)
        box.append(title)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("boxed-list")
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(list_box)
        scroll.set_vexpand(True)
        box.append(scroll)

        popover.set_child(box)
        self._popover = popover
        popover.present()

        # Cargar lista async.
        self._load_conversations_into(list_box, popover)

    def _load_conversations_into(
        self, list_box: Gtk.ListBox, popover: Gtk.Popover
    ) -> None:
        def runner() -> None:
            try:
                convs = self._client.list_conversations()
            except Exception as exc:  # noqa: BLE001
                logger.warning("list conversations: %s", exc)
                convs = []

            def render() -> bool:
                if not convs:
                    empty = Gtk.Label(label="Sin conversaciones aún.")
                    empty.add_css_class("dim-label")
                    list_box.append(empty)
                    return False
                for c in convs:
                    list_box.append(self._build_conv_row(c, popover))
                return False

            GLib.idle_add(render)

        threading.Thread(target=runner, daemon=True).start()

    def _build_conv_row(self, c: dict, popover: Gtk.Popover) -> Gtk.Widget:
        row = Adw.ActionRow()
        row.set_title(c.get("title", "(sin título)"))
        provider = c.get("provider_alias") or "?"
        model = c.get("model") or "?"
        msg_count = c.get("message_count", 0)
        row.set_subtitle(f"{provider} · {model} · {msg_count} msgs")
        row.set_activatable(True)
        row.connect(
            "activated",
            lambda _r: self._handle_open(c["conversation_id"], popover),
        )

        delete_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        delete_btn.set_valign(Gtk.Align.CENTER)
        delete_btn.add_css_class("flat")
        delete_btn.connect(
            "clicked",
            lambda _b: self._handle_delete(c["conversation_id"], row),
        )
        row.add_suffix(delete_btn)
        return row

    def _handle_open(self, conv_id: str, popover: Gtk.Popover) -> None:
        popover.popdown()
        self._on_open_conv(conv_id)

    def _handle_delete(self, conv_id: str, row: Gtk.Widget) -> None:
        def runner() -> None:
            try:
                self._client.delete_conversation(conversation_id=conv_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("delete conv: %s", exc)
                return
            GLib.idle_add(lambda: row.set_visible(False))

        threading.Thread(target=runner, daemon=True).start()
