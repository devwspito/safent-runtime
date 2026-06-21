"""PackagesPane — read-only list of installed apps on the LumenSO system.

Shows Flatpak or RPM packages queried through the RuntimeBridge.  The active
source is kept in self._source; pressing f/r swaps the source, fires a worker
that re-populates the DataTable, and updates the label.  No mutations in v1.
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable, Static

from hermes.tui.modals.common import ConfirmModal
from hermes.tui.modals.search_install import SearchInstallModal
from hermes.tui.screens.base import Pane
from hermes.tui.theme import PALETTE

_SOURCE_LABELS: dict[str, str] = {
    "flatpak": "Flatpak",
    "rpm": "RPM",
}


class PackagesPane(Pane):
    PANE_ID = "packages"
    TITLE = "Paquetes"
    SUBTITLE = "Apps instaladas en el sistema."

    BINDINGS = [
        Binding("f", "src_flatpak", "Flatpak"),
        Binding("r", "src_rpm", "RPM"),
        Binding("s", "search", "Buscar/Instalar"),
        Binding("x", "uninstall", "Desinstalar"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._source = "flatpak"
        self._packages: list[dict] = []

    # -- composition -------------------------------------------------------

    def build(self):
        yield Static(
            self._source_label_text(),
            id="pkg-source",
            classes="pane-subtitle",
        )
        table = DataTable(id="pkg-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Paquete", "Versión", "Origen")
        yield table
        yield Static(
            Text("f Flatpak · r RPM · s buscar/instalar · x desinstalar", style=PALETTE["text_faint"]),
            classes="pane-subtitle",
        )

    # -- lifecycle ---------------------------------------------------------

    async def activate(self) -> None:
        await self.safe_refresh()
        try:
            self.query_one("#pkg-table", DataTable).focus()
        except Exception:  # noqa: BLE001
            pass

    # -- data loading ------------------------------------------------------

    async def refresh_data(self) -> None:
        self._packages = await self.bridge.list_installed_packages(self._source)
        table = self.query_one("#pkg-table", DataTable)
        table.clear()
        if not self._packages:
            return
        for pkg in self._packages:
            name = str(
                pkg.get("name") or pkg.get("id") or pkg.get("app") or "—"
            )
            version = str(pkg.get("version") or pkg.get("ver") or "—")
            origin = str(
                pkg.get("source") or pkg.get("origin") or self._source
            )
            table.add_row(
                Text(name, style=PALETTE["text"]),
                Text(version, style=PALETTE["text_muted"]),
                Text(origin, style=PALETTE["text_faint"]),
                key=name,
            )

    # -- source switching --------------------------------------------------

    def _source_label_text(self) -> Text:
        label = _SOURCE_LABELS.get(self._source, self._source.upper())
        return Text.assemble(
            ("Origen: ", PALETTE["text_muted"]),
            (label, PALETTE["amber"]),
        )

    def _switch_source(self, source: str) -> None:
        self._source = source
        try:
            self.query_one("#pkg-source", Static).update(self._source_label_text())
        except Exception:  # noqa: BLE001
            pass
        self.run_worker(self.refresh_data(), exclusive=True)

    def action_src_flatpak(self) -> None:
        self._switch_source("flatpak")

    def action_src_rpm(self) -> None:
        self._switch_source("rpm")

    # -- search / install / uninstall (app store) -------------------------

    def action_search(self) -> None:
        async def _search(q: str) -> list[dict]:
            return await self.bridge.search_packages(q)

        async def _install(encoded: str) -> None:
            source, _, pkg_id = encoded.partition("::")
            await self.bridge.install_package(source or "flatpak", pkg_id or encoded)
            await self.refresh_data()

        self.app.push_screen(
            SearchInstallModal(
                title="Tienda de apps",
                placeholder="Buscar app (Flathub + dnf)… (Enter)",
                columns=("Paquete", "Versión", "Origen"),
                search_fn=_search,
                install_fn=_install,
                row_cells=lambda i: (
                    i.get("name") or i.get("id") or "—",
                    i.get("version") or "—",
                    i.get("source") or "—",
                ),
                row_id=lambda i: f"{i.get('source', 'flatpak')}::{i.get('name') or i.get('id')}",
            )
        )

    def _selected(self) -> dict | None:
        table = self.query_one("#pkg-table", DataTable)
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(self._packages):
            return None
        return self._packages[idx]

    def action_uninstall(self) -> None:
        pkg = self._selected()
        if not pkg:
            return
        name = str(pkg.get("name") or pkg.get("id") or "este paquete")
        self.app.push_screen(
            ConfirmModal("Desinstalar", f"¿Desinstalar «{name}»?", confirm_label="Desinstalar", danger=True),
            lambda ok: self.run_worker(self._uninstall(pkg), exclusive=True) if ok else None,
        )

    async def _uninstall(self, pkg: dict) -> None:
        pid = str(pkg.get("name") or pkg.get("id") or "")
        src = str(pkg.get("source") or self._source)
        try:
            await self.bridge.uninstall_package(src, pid)
            self.notify(f"«{pid}» desinstalado", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo desinstalar: {exc}", severity="error", timeout=6)
