"""SearchInstallModal — a reusable search → results → install marketplace modal.

Used by the Skills Hub and the Package store: type a query, see results in a
table, press Enter on a row to install. Keeps the two panes DRY and consistent.
The search/install are async callables injected by the caller (bridge-backed).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from hermes.tui.theme import PALETTE

Searcher = Callable[[str], Awaitable[list[dict]]]
Installer = Callable[[str], Awaitable[None]]
RowCells = Callable[[dict], tuple[str, ...]]
RowId = Callable[[dict], str]


class SearchInstallModal(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Cerrar"), ("enter", "install", "Instalar")]

    def __init__(
        self,
        *,
        title: str,
        placeholder: str,
        columns: tuple[str, ...],
        search_fn: Searcher,
        install_fn: Installer,
        row_cells: RowCells,
        row_id: RowId,
        install_label: str = "Instalar",
    ) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._columns = columns
        self._search_fn = search_fn
        self._install_fn = install_fn
        self._row_cells = row_cells
        self._row_id = row_id
        self._install_label = install_label
        self._results: list[dict] = []
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-card"):
            yield Static(Text(self._title, style="bold"), classes="modal-title")
            yield Input(placeholder=self._placeholder, id="si-search")
            table = DataTable(id="si-table", cursor_type="row", zebra_stripes=True)
            table.add_columns(*self._columns)
            yield table
            yield Static(
                Text("Enter buscar / instalar · Esc cerrar", style=PALETTE["text_faint"]),
                classes="modal-field",
            )

    def on_mount(self) -> None:
        self.query_one("#si-search", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "si-search":
            return
        query = event.value.strip()
        if not query:
            return
        self.run_worker(self._search(query), exclusive=True)

    async def _search(self, query: str) -> None:
        try:
            self._results = await self._search_fn(query)
        except Exception as exc:  # noqa: BLE001
            self.app.notify(f"Búsqueda falló: {exc}", severity="error", timeout=6)
            return
        table = self.query_one("#si-table", DataTable)
        table.clear()
        if not self._results:
            self.app.notify("Sin resultados", timeout=3)
            return
        for item in self._results:
            cells = tuple(Text(str(c), style=PALETTE["text"]) for c in self._row_cells(item))
            table.add_row(*cells, key=self._row_id(item))
        table.focus()

    def action_install(self) -> None:
        if self._busy:
            return
        # If focus is in the search box, Enter means "search", handled above.
        if self.focused is self.query_one("#si-search", Input):
            return
        table = self.query_one("#si-table", DataTable)
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(self._results):
            return
        item = self._results[idx]
        self._busy = True
        self.run_worker(self._install(self._row_id(item)), exclusive=True)

    async def _install(self, identifier: str) -> None:
        try:
            await self._install_fn(identifier)
            self.app.notify(f"«{identifier}» instalado", timeout=4)
        except Exception as exc:  # noqa: BLE001
            self.app.notify(f"No se pudo instalar: {exc}", severity="error", timeout=6)
        finally:
            self._busy = False

    def action_close(self) -> None:
        self.dismiss(None)
