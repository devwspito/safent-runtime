"""TasksPane — work queue + HITL approval queue.

Shows two DataTables:
  1. Recent tasks (historical + in-flight) from list_recent_tasks().
  2. Pending HITL proposals waiting for operator approval from list_hitl_pending().

Approve/reject flows reuse ApprovalModal so the HITL UX is identical to the
QML SecurityApprovalCard: same daemon verbs, same authorship model.
"""

from __future__ import annotations

import json

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable, Static

from hermes.tui.modals.approval import ApprovalModal
from hermes.tui.screens.base import Pane
from hermes.tui.theme import PALETTE, risk_color


def _status_color(status: str) -> str:
    """Map a task status string to a palette color."""
    normalized = (status or "").strip().lower()
    if normalized in ("running", "in_progress"):
        return PALETTE["amber"]
    if normalized in ("done", "completed"):
        return PALETTE["success"]
    if normalized in ("error", "failed"):
        return PALETTE["error"]
    return PALETTE["text_muted"]


def _fmt_when(item: dict) -> str:
    """Return the most available timestamp field, or a dash."""
    return str(item.get("created_at") or item.get("when") or "—")


class TasksPane(Pane):
    PANE_ID = "tasks"
    TITLE = "Tareas"
    SUBTITLE = "Cola de trabajo y actividad reciente."

    BINDINGS = [
        Binding("a", "approve", "Aprobar"),
        Binding("enter", "approve", "", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._tasks: list[dict] = []
        self._hitl: list[dict] = []

    def build(self):
        # Summary card: queue state at a glance.
        yield Static("", id="tasks-summary", classes="card")

        # Recent tasks table.
        tasks_table = DataTable(
            id="tasks-table",
            cursor_type="row",
            zebra_stripes=True,
        )
        tasks_table.add_columns("Tarea", "Tipo", "Estado", "Cuándo")
        yield tasks_table

        # HITL section header.
        yield Static("Pendiente de tu aprobación", classes="pane-subtitle")

        # HITL approval table.
        hitl_table = DataTable(id="hitl-table", cursor_type="row")
        hitl_table.add_columns("Acción", "Riesgo", "Propuesta")
        yield hitl_table

    async def activate(self) -> None:
        await self.safe_refresh()
        try:
            hitl = self.query_one("#hitl-table", DataTable)
            tasks = self.query_one("#tasks-table", DataTable)
            if self._hitl:
                hitl.focus()
            else:
                tasks.focus()
        except Exception:  # noqa: BLE001
            pass

    async def refresh_data(self) -> None:
        status = await self.bridge.get_queue_status()
        self._tasks = await self.bridge.list_recent_tasks(50)
        self._hitl = await self.bridge.list_hitl_pending(50)

        self._update_summary(status)
        self._populate_tasks_table()
        self._populate_hitl_table()

    def _update_summary(self, status: dict) -> None:
        state = str(status.get("state") or "—")
        pending = status.get("pending", 0)
        in_progress = status.get("in_progress", 0)
        pending_approval = status.get("pending_approval", 0)

        summary = Text()
        summary.append("Estado: ", style=PALETTE["text_muted"])
        summary.append(state, style=PALETTE["amber"])
        summary.append("  ·  pendientes ", style=PALETTE["text_muted"])
        summary.append(str(pending), style=PALETTE["text"])
        summary.append("  ·  en curso ", style=PALETTE["text_muted"])
        summary.append(str(in_progress), style=PALETTE["text"])
        summary.append("  ·  esperando aprobación ", style=PALETTE["text_muted"])
        approval_style = PALETTE["error"] if pending_approval else PALETTE["text"]
        summary.append(str(pending_approval), style=approval_style)

        try:
            self.query_one("#tasks-summary", Static).update(summary)
        except Exception:  # noqa: BLE001
            pass

    def _populate_tasks_table(self) -> None:
        try:
            table = self.query_one("#tasks-table", DataTable)
        except Exception:  # noqa: BLE001
            return
        table.clear()
        if not self._tasks:
            return
        for item in self._tasks:
            title = str(item.get("title") or item.get("summary") or item.get("task_id") or "—")
            kind = str(item.get("trigger_kind") or item.get("kind") or "—")
            status = str(item.get("status") or "—")
            when = _fmt_when(item)
            sc = _status_color(status)
            table.add_row(
                Text(title, style=PALETTE["text"]),
                Text(kind, style=PALETTE["text_muted"]),
                Text(status, style=sc),
                Text(when, style=PALETTE["text_faint"]),
            )

    def _populate_hitl_table(self) -> None:
        try:
            table = self.query_one("#hitl-table", DataTable)
        except Exception:  # noqa: BLE001
            return
        table.clear()
        if not self._hitl:
            return
        for item in self._hitl:
            action = str(item.get("tool") or item.get("action") or "—")
            risk = str(item.get("risk") or "—")
            proposal_id = str(item.get("proposal_id") or "—")
            rc = risk_color(risk)
            table.add_row(
                Text(action, style=PALETTE["text"]),
                Text(risk.upper(), style=rc),
                Text(proposal_id[:24] + ("…" if len(proposal_id) > 24 else ""), style=PALETTE["text_muted"]),
            )

    # -- selection helper -------------------------------------------------

    def _selected_hitl(self) -> dict | None:
        try:
            table = self.query_one("#hitl-table", DataTable)
        except Exception:  # noqa: BLE001
            return None
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(self._hitl):
            return None
        return self._hitl[idx]

    # -- approve action ---------------------------------------------------

    def action_approve(self) -> None:
        item = self._selected_hitl()
        if item is None:
            self.notify("No hay nada que aprobar.", timeout=4)
            return
        modal = ApprovalModal(self.bridge, json.dumps(item))
        self.app.push_screen(modal, self._on_approval_result)

    def _on_approval_result(self, _result: bool | None) -> None:
        self.run_worker(self._refresh_after_approval(), exclusive=True)

    async def _refresh_after_approval(self) -> None:
        try:
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo actualizar: {exc}", severity="error", timeout=6)
