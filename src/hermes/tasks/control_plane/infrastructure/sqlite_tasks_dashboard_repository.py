"""SqliteTasksDashboardRepository — read model for `GET /api/v1/tasks/dashboard`
(docs/logica-pendiente-2026-09-11.md §1 / SAFENT-PENDIENTES.md §5).

CQRS read-only projection over `agent_tasks` + `pending_delegations` +
`pending_approvals` + `messages` — all in the SAME shell-state.db file
(same pattern as `SQLiteConversationRepository`'s mirror read-only). This
module owns NO write path and enforces NO invariant: those stay with their
respective bounded contexts (`tasks.infrastructure.sqlite_work_queue`,
`tasks.infrastructure.sqlite_pending_delegations`,
`capabilities.infrastructure.sqlite_approval_gate`).

Fail-closed contract (spec, verbatim): "Fallo de fuente -> 503, no `[]`
exitoso". The PRIMARY source (`agent_tasks` LEFT JOIN `pending_delegations`,
which together resolve `source`) raises `TasksDashboardUnavailable` on ANY
sqlite error — the route MUST translate this to HTTP 503, never a silent
empty list. The two ENRICHMENT reads (`pending_approvals` for
`approval_ids`, `messages` for `result`) degrade independently and
per-field: a missing/broken table there yields `approval_ids=None`
("desconocido", never a false `()`) or an absent `result`, WITHOUT failing
the whole request — exactly the "ausente = desconocido" semantics the spec
draws for `approval_ids`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes.tasks.control_plane.domain.ports import DashboardTaskView
from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (
    _extract_instruction,
)


class TasksDashboardUnavailable(RuntimeError):
    """The durable read source could not be queried. Callers MUST translate
    this to HTTP 503 — never degrade to a false empty success."""


class SqliteTasksDashboardRepository:
    """Connection-per-call reader (autocommit) over shell-state.db.

    Mirrors `SQLiteConversationRepository`'s pattern: no ORM, no framework,
    parameterized SQL only.
    """

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path

    def list_dashboard_tasks(
        self, *, limit: int
    ) -> tuple[list[DashboardTaskView], bool]:
        """Returns (rows newest-first, has_more). `limit+1` fetch trick for
        `has_more` (spec: "limit+1 para has_more").

        Raises:
            TasksDashboardUnavailable: primary source (agent_tasks/
                pending_delegations) unreachable or malformed.
        """
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise TasksDashboardUnavailable(str(exc)) from exc
        try:
            return self._query(conn, limit=limit)
        except sqlite3.Error as exc:
            raise TasksDashboardUnavailable(str(exc)) from exc
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _query(
        self, conn: sqlite3.Connection, *, limit: int
    ) -> tuple[list[DashboardTaskView], bool]:
        rows = conn.execute(
            """
            SELECT
                t.task_id, t.status, t.payload_json, t.enqueued_by,
                t.created_at, t.updated_at, t.conversation_id,
                d.message_id AS delegation_message_id
            FROM agent_tasks t
            LEFT JOIN pending_delegations d ON d.task_id = t.task_id
            ORDER BY t.created_at DESC, t.task_id DESC
            LIMIT ?
            """,
            (limit + 1,),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        task_ids = [r["task_id"] for r in rows]
        approvals = self._approval_ids_by_task(conn, task_ids)
        results = self._completed_results_by_task(
            conn, [r["task_id"] for r in rows if r["status"] == "completed"]
        )
        views = [self._row_to_view(r, approvals, results) for r in rows]
        return views, has_more

    def _approval_ids_by_task(
        self, conn: sqlite3.Connection, task_ids: list[str]
    ) -> dict[str, list[str]] | None:
        """`None` = the evidence source could not be checked at all (every
        row in this page gets `approval_ids=None` — "desconocido"). A dict
        (possibly with a task missing) means checked; a task absent from it
        means checked-empty ("comprobado sin vínculos")."""
        if not task_ids:
            return {}
        placeholders = ",".join("?" for _ in task_ids)
        try:
            rows = conn.execute(
                f"SELECT work_item_id, proposal_id FROM pending_approvals "  # noqa: S608
                f"WHERE work_item_id IN ({placeholders})",
                task_ids,
            ).fetchall()
        except sqlite3.Error:
            return None
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["work_item_id"], []).append(r["proposal_id"])
        return out

    def _completed_results_by_task(
        self, conn: sqlite3.Connection, task_ids: list[str]
    ) -> dict[str, str]:
        """Latest assistant message per completed task_id (same pattern as
        `config_sync.delegation_inbox._fetch_unpushed_delegation_results` —
        "Resultado final conserva ruta existente"). Missing/broken `messages`
        degrades to no results for this page — `result` is optional."""
        if not task_ids:
            return {}
        placeholders = ",".join("?" for _ in task_ids)
        try:
            rows = conn.execute(
                f"""
                SELECT m.task_id, m.content
                FROM messages m
                WHERE m.task_id IN ({placeholders})
                  AND m.role = 'assistant'
                  AND m.message_id = (
                      SELECT m2.message_id FROM messages m2
                      WHERE m2.task_id = m.task_id AND m2.role = 'assistant'
                      ORDER BY m2.created_at DESC, m2.message_id DESC
                      LIMIT 1
                  )
                """,  # noqa: S608
                task_ids,
            ).fetchall()
        except sqlite3.Error:
            return {}
        return {r["task_id"]: r["content"] for r in rows}

    def _row_to_view(
        self,
        row: sqlite3.Row,
        approvals: dict[str, list[str]] | None,
        results: dict[str, str],
    ) -> DashboardTaskView:
        task_id = row["task_id"]
        source = "enterprise" if row["delegation_message_id"] is not None else "local"
        approval_ids = (
            None if approvals is None else tuple(approvals.get(task_id, ()))
        )
        return DashboardTaskView(
            task_id=task_id,
            label=_extract_instruction(row["payload_json"]),
            status=row["status"],
            source=source,
            requested_by=row["enqueued_by"] or None,
            created_at=row["created_at"] or None,
            updated_at=row["updated_at"] or None,
            conversation_id=row["conversation_id"],
            result=results.get(task_id),
            approval_ids=approval_ids,
        )
