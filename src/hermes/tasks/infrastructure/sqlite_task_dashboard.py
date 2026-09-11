"""Read-only, durable task projection for the local owner's dashboard.

Never infer enterprise provenance from a prompt or delivery ACK. Missing optional
stores mean unknown fields, not fabricated results or approvals. Storage failures
propagate so the HTTP surface can report unavailable instead of an empty inbox.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_STATUSES = {
    "pending",
    "in_progress",
    "completed",
    "failed",
    "pending_approval",
    "rejected",
    "cancelled",
}
_MAX_LIMIT = 200
_RESULT_LIMIT = 8000


def read_task_dashboard(db_path: Path, *, limit: int = 100) -> dict:
    if isinstance(limit, bool) or not 1 <= limit <= _MAX_LIMIT:
        raise ValueError("dashboard limit must be between 1 and 200")
    # mode=ro prevents a mistyped/missing path becoming a successful empty DB.
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        has_delegations = "pending_delegations" in tables
        join = ""
        origin = "NULL AS requested_by, 'local' AS source"
        union = ""
        if has_delegations:
            # Multiple requests pointing at one execution is an integrity error,
            # never pick whichever identity the query happens to return first.
            duplicate = conn.execute(
                "SELECT task_id FROM pending_delegations WHERE task_id IS NOT NULL "
                "GROUP BY task_id HAVING COUNT(*) > 1 LIMIT 1"
            ).fetchone()
            if duplicate:
                raise ValueError("ambiguous delegation task provenance")
            join = "LEFT JOIN pending_delegations d ON d.task_id=t.task_id"
            origin = (
                "d.from_employee_id AS requested_by, "
                "CASE WHEN d.message_id IS NULL THEN 'local' ELSE 'enterprise' END AS source"
            )
            union = """
                UNION ALL
                SELECT 'delegation:' || message_id, substr(body,1,120),
                    CASE status WHEN 'pending' THEN 'pending_approval' ELSE 'rejected' END,
                    created_at, COALESCE(resolved_at,created_at), conversation_id,
                    from_employee_id, 'enterprise'
                FROM pending_delegations
                WHERE task_id IS NULL AND status IN ('pending','rejected')
            """
        rows = conn.execute(
            f"""SELECT t.task_id AS task_id, substr(t.instruction,1,120) AS label, t.status,
                t.created_at, t.updated_at, t.conversation_id, {origin}
                FROM agent_tasks t {join}
                {union}
                ORDER BY created_at DESC, task_id DESC LIMIT ?""",  # noqa: S608 — static fragments only
            (limit + 1,),
        ).fetchall()
        tasks = []
        for row in rows[:limit]:
            item = dict(row)
            if item["status"] not in _STATUSES:
                raise ValueError("unsupported persisted task status")
            if "messages" in tables and item["status"] == "completed":
                message = conn.execute(
                    "SELECT substr(content,1,?), length(content) FROM messages "
                    "WHERE task_id=? AND role='assistant' "
                    "ORDER BY created_at DESC, message_id DESC LIMIT 1",
                    (_RESULT_LIMIT, item["task_id"]),
                ).fetchone()
                if message:
                    item["result"] = message[0]
                    if message[1] > _RESULT_LIMIT:
                        item["result"] += "\n[… Ver conversación para el resultado completo.]"
            if "pending_approvals" in tables and not item["task_id"].startswith("delegation:"):
                item["approval_ids"] = [
                    entry[0]
                    for entry in conn.execute(
                        "SELECT proposal_id FROM pending_approvals WHERE work_item_id=? "
                        "ORDER BY proposal_id",
                        (item["task_id"],),
                    )
                ]
            tasks.append(item)
        return {"available": True, "tasks": tasks, "has_more": len(rows) > limit}
    finally:
        conn.close()
