"""Sidebar — primary navigation between panes.

Posts Sidebar.Navigate(pane_id) when the selection changes; the app swaps the
ContentSwitcher. Keyboard-first (j/k/arrows, Enter); the command palette and
number keys also jump directly.

Layout: 6 primary entries (chat-first) + a collapsible "Avanzado" group
containing the 5 advanced panes. Collapsed by default so a first-time user
sees a clean, focused list.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Label, ListItem, ListView, Static

from hermes.tui.theme import PALETTE


@dataclass(frozen=True)
class NavEntry:
    pane_id: str
    icon: str
    label: str
    key: str  # quick-jump digit


# Primary visible entries (shown by default, in product-owner order).
NAV_PRIMARY: tuple[NavEntry, ...] = (
    NavEntry("chat", "✦", "Cerebro", "1"),
    NavEntry("skills", "✧", "Skills", "2"),
    NavEntry("integrations", "⇄", "Integraciones", "3"),
    NavEntry("mcp", "⊞", "MCP", "4"),
    NavEntry("agents", "◇", "Agentes", "5"),
    NavEntry("tasks", "≣", "Tareas", "6"),
)

# Advanced entries — shown only when Avanzado is expanded.
NAV_ADVANCED: tuple[NavEntry, ...] = (
    NavEntry("security", "⛨", "Seguridad", "7"),
    NavEntry("scheduler", "◷", "Programador", "8"),
    NavEntry("memory", "❖", "Memoria", "9"),
    NavEntry("providers", "⚙", "Proveedores", "0"),
    NavEntry("packages", "▤", "Paquetes", "—"),
)

# Combined tuple used by the command palette and select_pane.
NAV: tuple[NavEntry, ...] = NAV_PRIMARY + NAV_ADVANCED


class Sidebar(Vertical):
    class Navigate(Message):
        def __init__(self, pane_id: str) -> None:
            self.pane_id = pane_id
            super().__init__()

    def __init__(self) -> None:
        super().__init__()
        self._advanced_open = False

    def compose(self) -> ComposeResult:
        yield Static("NAVEGACIÓN", classes="nav-title")
        primary_items = []
        for e in NAV_PRIMARY:
            primary_items.append(ListItem(Label(self._entry_text(e)), id=f"nav-{e.pane_id}"))
        yield ListView(*primary_items, id="nav-list")

        # Avanzado toggle header.
        yield Static(
            self._avanzado_header(),
            id="nav-avanzado-toggle",
            classes="nav-avanzado-toggle",
        )

        # Advanced entries list — hidden by default.
        adv_items = []
        for e in NAV_ADVANCED:
            adv_items.append(ListItem(Label(self._entry_text(e)), id=f"nav-{e.pane_id}"))
        adv_list = ListView(*adv_items, id="nav-list-adv")
        adv_list.display = False
        yield adv_list

    def _entry_text(self, e: NavEntry) -> Text:
        line = Text()
        line.append(f"{e.icon}  ", style=PALETTE["amber"])
        line.append(e.label, style=PALETTE["text"])
        line.append(f"  {e.key}", style=PALETTE["text_faint"])
        return line

    def _avanzado_header(self) -> Text:
        chevron = "▾" if self._advanced_open else "▸"
        t = Text()
        t.append(f"{chevron} ", style=PALETTE["text_muted"])
        t.append("Avanzado", style=PALETTE["text_muted"])
        return t

    # -- event handlers ---------------------------------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None and event.item.id:
            self.post_message(self.Navigate(event.item.id.removeprefix("nav-")))

    def on_static_click(self, event: Static.Clicked) -> None:  # type: ignore[name-defined]
        if event.widget.id == "nav-avanzado-toggle":
            self._toggle_advanced()

    def on_click(self, event) -> None:  # noqa: ANN001
        # Textual routes click on child widgets as well; guard by id.
        target = getattr(event, "widget", None)
        if target is not None and getattr(target, "id", None) == "nav-avanzado-toggle":
            self._toggle_advanced()

    def on_key(self, event) -> None:  # noqa: ANN001
        # Allow toggling Avanzado with 'a' when the sidebar has focus.
        if event.key == "a":
            self._toggle_advanced()

    def _toggle_advanced(self) -> None:
        self._advanced_open = not self._advanced_open
        try:
            adv_list = self.query_one("#nav-list-adv", ListView)
            adv_list.display = self._advanced_open
            toggle = self.query_one("#nav-avanzado-toggle", Static)
            toggle.update(self._avanzado_header())
        except Exception:  # noqa: BLE001
            pass

    # -- programmatic selection -------------------------------------------

    def select_pane(self, pane_id: str) -> None:
        """Highlight the sidebar entry for pane_id, expanding Avanzado if needed."""
        # Check primary list first.
        for idx, e in enumerate(NAV_PRIMARY):
            if e.pane_id == pane_id:
                self.query_one("#nav-list", ListView).index = idx
                return
        # Pane is in the advanced group — ensure the section is open.
        for idx, e in enumerate(NAV_ADVANCED):
            if e.pane_id == pane_id:
                if not self._advanced_open:
                    self._toggle_advanced()
                try:
                    self.query_one("#nav-list-adv", ListView).index = idx
                except Exception:  # noqa: BLE001
                    pass
                return
