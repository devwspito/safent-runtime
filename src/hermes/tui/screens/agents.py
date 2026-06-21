"""AgentsPane — Cerebro (omnipotent, not editable) + your custom agents.

Reference implementation for every data pane: DataTable list + row actions via
the shared FormModal / ConfirmModal, async work through run_worker, honest empty
state, errors surfaced with notify. Mirrors the agent-architecture model: the
default agent is the Cerebro — activatable but never edited or deleted here.
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable, Static

from hermes.tui.modals.common import ConfirmModal, Field, FormModal
from hermes.tui.screens.base import Pane
from hermes.tui.theme import PALETTE


class AgentsPane(Pane):
    PANE_ID = "agents"
    TITLE = "Agentes"
    SUBTITLE = "Cerebro (omnipotente) y tus agentes personalizados."

    BINDINGS = [
        Binding("n", "new_agent", "Nuevo"),
        Binding("e", "edit_agent", "Editar"),
        Binding("d", "delete_agent", "Borrar"),
        Binding("enter", "activate_agent", "Activar", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._agents: list[dict] = []
        self._active_id = ""

    def build(self):
        table = DataTable(id="agents-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("", "Agente", "Rol", "Autonomía", "Estado")
        yield table
        yield Static(
            Text(
                "enter activar · n nuevo · e editar · d borrar  ·  "
                "♛ = Cerebro (no editable)",
                style=PALETTE["text_faint"],
            ),
            id="agents-help",
        )

    async def activate(self) -> None:
        await self.safe_refresh()
        try:
            self.query_one("#agents-table", DataTable).focus()
        except Exception:  # noqa: BLE001
            pass

    async def refresh_data(self) -> None:
        self._agents = await self.bridge.list_agents()
        self._active_id = await self.bridge.get_active_agent()
        table = self.query_one("#agents-table", DataTable)
        table.clear()
        if not self._agents:
            return
        for a in self._agents:
            is_default = bool(a.get("is_default"))
            is_active = str(a.get("id")) == self._active_id
            marker = "♛" if is_default else ("●" if is_active else "·")
            marker_style = PALETTE["amber"] if (is_default or is_active) else PALETTE["text_faint"]
            estado = (
                "Cerebro · omnipotente"
                if is_default
                else ("activo" if is_active else "—")
            )
            table.add_row(
                Text(marker, style=marker_style),
                Text(str(a.get("name", "—")), style=PALETTE["text"]),
                Text(str(a.get("role", "")) or "—", style=PALETTE["text_muted"]),
                Text(str(a.get("autonomy_level", "")) or "—", style=PALETTE["text_muted"]),
                Text(estado, style=marker_style if (is_default or is_active) else PALETTE["text_muted"]),
                key=str(a.get("id")),
            )

    # -- selection helpers ------------------------------------------------
    def _selected(self) -> dict | None:
        table = self.query_one("#agents-table", DataTable)
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(self._agents):
            return None
        return self._agents[idx]

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_activate_agent()

    # -- actions ----------------------------------------------------------
    def action_activate_agent(self) -> None:
        agent = self._selected()
        if not agent:
            return
        self.run_worker(self._activate(agent), exclusive=True)

    async def _activate(self, agent: dict) -> None:
        try:
            await self.bridge.set_active_agent(str(agent["id"]))
            self.notify(f"Agente activo: {agent.get('name')}", timeout=3)
            await self.refresh_data()
            # reflect in the header
            bar = self.app.query_one("StatusBar")
            bar.agent_name = str(agent.get("name", "Lumen"))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo activar: {exc}", severity="error", timeout=6)

    def action_new_agent(self) -> None:
        fields = [
            Field("name", "Nombre", "p.ej. Analista de facturas", required=True),
            Field("role", "Rol", "p.ej. Contable junior"),
            Field("mission", "Misión", "qué objetivo persigue"),
            Field("instructions", "Instrucciones", "cómo debe trabajar"),
        ]
        self.app.push_screen(
            FormModal("Nuevo agente", fields, save_label="Crear",
                      note="Los agentes nuevos heredan tus permisos; los apretarás luego."),
            self._on_new_result,
        )

    def _on_new_result(self, values: dict | None) -> None:
        if values is None:
            return
        self.run_worker(self._create(values), exclusive=True)

    async def _create(self, values: dict) -> None:
        try:
            await self.bridge.create_agent(values)
            self.notify(f"Agente «{values.get('name')}» creado", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo crear: {exc}", severity="error", timeout=6)

    def action_edit_agent(self) -> None:
        agent = self._selected()
        if not agent:
            return
        if agent.get("is_default"):
            self.notify("El Cerebro es omnipotente: no se edita.", severity="warning", timeout=4)
            return
        fields = [
            Field("name", "Nombre", value=str(agent.get("name", "")), required=True),
            Field("role", "Rol", value=str(agent.get("role", ""))),
            Field("mission", "Misión", value=str(agent.get("mission", ""))),
            Field("instructions", "Instrucciones", value=str(agent.get("instructions", ""))),
        ]
        self.app.push_screen(
            FormModal(f"Editar · {agent.get('name')}", fields, save_label="Guardar"),
            lambda values, a=agent: self._on_edit_result(a, values),
        )

    def _on_edit_result(self, agent: dict, values: dict | None) -> None:
        if values is None:
            return
        self.run_worker(self._update(agent, values), exclusive=True)

    async def _update(self, agent: dict, values: dict) -> None:
        try:
            await self.bridge.update_agent(str(agent["id"]), values)
            self.notify("Agente actualizado", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo actualizar: {exc}", severity="error", timeout=6)

    def action_delete_agent(self) -> None:
        agent = self._selected()
        if not agent:
            return
        if agent.get("is_default"):
            self.notify("El Cerebro no se puede borrar.", severity="warning", timeout=4)
            return
        self.app.push_screen(
            ConfirmModal(
                "Borrar agente",
                f"¿Borrar «{agent.get('name')}»? Esta acción no se puede deshacer.",
                confirm_label="Borrar",
                danger=True,
            ),
            lambda ok, a=agent: self._on_delete_result(a, ok),
        )

    def _on_delete_result(self, agent: dict, ok: bool | None) -> None:
        if not ok:
            return
        self.run_worker(self._delete(agent), exclusive=True)

    async def _delete(self, agent: dict) -> None:
        try:
            await self.bridge.delete_agent(str(agent["id"]))
            self.notify("Agente borrado", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo borrar: {exc}", severity="error", timeout=6)
