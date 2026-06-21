"""SchedulerPane — programmed (cron) tasks per agent.

Lists all configured scheduled triggers via list_configured_tasks, allows
creating new ones with a cron expression form, toggling enabled/disabled, and
deleting them after a danger confirm. Follows the reference contract defined by
AgentsPane in agents.py exactly: build/refresh_data/activate/run_worker/
push_screen/notify.
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable, Static

from hermes.tui.modals.common import ConfirmModal, Field, FormModal
from hermes.tui.screens.base import Pane
from hermes.tui.theme import PALETTE

_CRON_NOTE = (
    "Cron de 5 campos: min hora díames mes díasemana. "
    "Ej: 0 18 * * 1,3,5 = Lun/Mié/Vie 18:00."
)


class SchedulerPane(Pane):
    PANE_ID = "scheduler"
    TITLE = "Programador"
    SUBTITLE = "Tareas programadas (cron) por agente."

    BINDINGS = [
        Binding("n", "new_task", "Nueva"),
        Binding("t", "toggle_task", "Activar/Desactivar"),
        Binding("d", "delete_task", "Borrar"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._tasks: list[dict] = []

    # -- compose body -------------------------------------------------------

    def build(self):
        yield DataTable(id="sched-table", cursor_type="row", zebra_stripes=True)
        yield Static(
            Text(
                "n nueva · t activar/desactivar · d borrar",
                style=PALETTE["text_faint"],
            ),
            id="sched-help",
            classes="pane-subtitle",
        )

    def on_mount(self) -> None:
        # add_columns requires the app context; on_mount runs inside the event loop.
        table = self.query_one("#sched-table", DataTable)
        table.add_columns("Tarea", "Cuándo (cron)", "Agente", "Activa")

    # -- lifecycle ----------------------------------------------------------

    async def activate(self) -> None:
        await self.safe_refresh()
        try:
            self.query_one("#sched-table", DataTable).focus()
        except Exception:  # noqa: BLE001
            pass

    async def refresh_data(self) -> None:
        self._tasks = await self.bridge.list_configured_tasks(50)
        table = self.query_one("#sched-table", DataTable)
        table.clear()
        if not self._tasks:
            return
        for task in self._tasks:
            enabled = bool(task.get("enabled", True))
            agent_raw = (
                task.get("target_agent_id")
                or task.get("agent")
                or ""
            )
            # Show a short UUID prefix or "Cerebro" when no agent is targeted.
            if agent_raw:
                agent_label = str(agent_raw)[:8]
            else:
                agent_label = "Cerebro"

            activa_text = (
                Text("● sí", style=PALETTE["success"])
                if enabled
                else Text("○ no", style=PALETTE["text_faint"])
            )
            table.add_row(
                Text(str(task.get("title") or task.get("name") or "—"), style=PALETTE["text"]),
                Text(str(task.get("cron") or task.get("schedule") or "—"), style=PALETTE["text_muted"]),
                Text(agent_label, style=PALETTE["text_muted"]),
                activa_text,
                key=str(task.get("trigger_id") or task.get("id") or ""),
            )

    # -- selection helper ---------------------------------------------------

    def _selected(self) -> dict | None:
        table = self.query_one("#sched-table", DataTable)
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(self._tasks):
            return None
        return self._tasks[idx]

    # -- action: new task ---------------------------------------------------

    def action_new_task(self) -> None:
        fields = [
            Field("title", "Título", "p.ej. Revisar facturas", required=True),
            Field("task_instruction", "Instrucción", "qué debe hacer el agente", required=True),
            Field("cron", "Cron", "0 18 * * 1,3,5", required=True),
            Field("target_agent_id", "Agente (id, opcional)", "vacío = Cerebro"),
        ]
        self.app.push_screen(
            FormModal(
                "Nueva tarea programada",
                fields,
                save_label="Programar",
                note=_CRON_NOTE,
            ),
            self._on_new_result,
        )

    def _on_new_result(self, values: dict | None) -> None:
        if values is None:
            return
        self.run_worker(self._create(values), exclusive=True)

    async def _create(self, values: dict) -> None:
        draft: dict = {
            "title": values["title"],
            "task_instruction": values["task_instruction"],
            "cron": values["cron"],
        }
        target = values.get("target_agent_id", "").strip()
        if target:
            draft["target_agent_id"] = target
        try:
            await self.bridge.create_scheduled_task(draft)
            self.notify(f"Tarea «{draft['title']}» programada", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo crear: {exc}", severity="error", timeout=6)

    # -- action: toggle enabled --------------------------------------------

    def action_toggle_task(self) -> None:
        task = self._selected()
        if not task:
            return
        self.run_worker(self._toggle(task), exclusive=True)

    async def _toggle(self, task: dict) -> None:
        trigger_id = str(task.get("trigger_id") or task.get("id") or "")
        enabled = bool(task.get("enabled", True))
        try:
            await self.bridge.set_scheduled_task_enabled(trigger_id, not enabled)
            state_label = "desactivada" if enabled else "activada"
            title = task.get("title") or task.get("name") or trigger_id
            self.notify(f"Tarea «{title}» {state_label}", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo cambiar estado: {exc}", severity="error", timeout=6)

    # -- action: delete task -----------------------------------------------

    def action_delete_task(self) -> None:
        task = self._selected()
        if not task:
            return
        title = task.get("title") or task.get("name") or "esta tarea"
        self.app.push_screen(
            ConfirmModal(
                "Borrar tarea",
                f"¿Borrar «{title}»? Esta acción no se puede deshacer.",
                confirm_label="Borrar",
                danger=True,
            ),
            lambda ok, t=task: self._on_delete_result(t, ok),
        )

    def _on_delete_result(self, task: dict, ok: bool | None) -> None:
        if not ok:
            return
        self.run_worker(self._delete(task), exclusive=True)

    async def _delete(self, task: dict) -> None:
        trigger_id = str(task.get("trigger_id") or task.get("id") or "")
        title = task.get("title") or task.get("name") or trigger_id
        try:
            await self.bridge.delete_scheduled_task(trigger_id)
            self.notify(f"Tarea «{title}» borrada", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo borrar: {exc}", severity="error", timeout=6)
