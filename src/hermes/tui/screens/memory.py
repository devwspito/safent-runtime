"""MemoryPane — what the agent remembers.

Read-only list of memory entries with a live search box. Mirrors the
AgentsPane contract: DataTable + Input, async bridge calls via run_worker,
honest empty state, errors surfaced with notify.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import DataTable, Input, Static

from hermes.tui.screens.base import Pane
from hermes.tui.theme import PALETTE

_CONTENT_MAX = 80


def _truncate(value: str) -> str:
    return value if len(value) <= _CONTENT_MAX else value[:_CONTENT_MAX] + "…"


class MemoryPane(Pane):
    PANE_ID = "memory"
    TITLE = "Memoria"
    SUBTITLE = "Lo que el agente recuerda."

    def build(self):
        yield Input(
            id="mem-search",
            placeholder="Buscar en la memoria…  (Enter)",
        )
        yield DataTable(id="mem-table", cursor_type="row", zebra_stripes=True)
        yield Static(
            Text(
                "enter buscar · vacío restablecer lista completa",
                style=PALETTE["text_faint"],
            ),
            id="mem-help",
        )

    def on_mount(self) -> None:
        self.query_one("#mem-table", DataTable).add_columns("#", "Objetivo", "Contenido")

    async def activate(self) -> None:
        await self.safe_refresh()
        try:
            self.query_one("#mem-search", Input).focus()
        except Exception:  # noqa: BLE001
            pass

    async def refresh_data(self) -> None:
        rows = await self.bridge.list_memory(100)
        self._fill(rows)

    def _fill(self, rows: list[dict]) -> None:
        table = self.query_one("#mem-table", DataTable)
        table.clear()
        if not rows:
            return
        for entry in rows:
            index = str(
                entry.get("entry_index") or entry.get("id") or ""
            )
            target = str(entry.get("target") or "—")
            raw_content = str(
                entry.get("content_truncated")
                or entry.get("content")
                or entry.get("text")
                or "—"
            )
            table.add_row(
                Text(index, style=PALETTE["text_faint"]),
                Text(target, style=PALETTE["amber"]),
                Text(_truncate(raw_content), style=PALETTE["text_muted"]),
                key=index,
            )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "mem-search":
            return
        query = event.value.strip()
        if query:
            self.run_worker(self._search(query), exclusive=True)
        else:
            self.run_worker(self.refresh_data(), exclusive=True)

    async def _search(self, query: str) -> None:
        try:
            rows = await self.bridge.search_memory(query, 100)
            self._fill(rows)
            if not rows:
                self.notify("Sin resultados para esa búsqueda.", timeout=3)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Error al buscar: {exc}", severity="error", timeout=6)
