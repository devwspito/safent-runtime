"""SqlitePendingDelegationRepository — buzón durable de delegaciones entrantes
(FASE 3 A2A cross-human, RUNTIME/associate side).

Una fila = UNA DelegationEnvelope kind=request, ya VERIFICADA por firma por
`config_sync.delegation_inbox` (tenant pubkey), pendiente de que el humano
LOCAL apruebe o rechace ("El asistente de <from_employee_id> te pide: «body» —
Aprobar/Rechazar"). Vive en shell-state.db (misma DB que `agent_tasks` /
`pending_approvals` / `authorized_trigger_instances`) — bounded context propio
(`tasks/triggers`), tabla propia (NO reutiliza `pending_approvals`, que modela
la reanudación de un tool-call NATIVO bloqueado, una semántica distinta a
"encolar un WorkItem nuevo a partir de una petición de un par").

Idempotencia: PK = message_id (el id que el CLOUD asigna a la DelegationEnvelope
— NO uno nuestro). Un `submit` repetido (p.ej. config_sync reintenta tras un
ack fallido) es un INSERT OR IGNORE — nunca duplica la tarjeta.

Fail-closed: cualquier error de SQLite en una lectura devuelve None/[]  (nunca
propaga una excepción que podría interpretarse como "aprobado").
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DDL_PENDING_DELEGATIONS = """
CREATE TABLE IF NOT EXISTS pending_delegations (
    message_id       TEXT PRIMARY KEY,
    correlation_id   TEXT NOT NULL,
    from_employee_id TEXT NOT NULL,
    from_agent_id    TEXT NOT NULL DEFAULT '',
    from_instance_id TEXT NOT NULL,
    to_employee_id   TEXT NOT NULL DEFAULT '',
    to_agent_id      TEXT NOT NULL DEFAULT '',
    body             TEXT NOT NULL,
    issued_at        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    resolved_by      TEXT,
    resolved_at      TEXT,
    task_id          TEXT,
    conversation_id  TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_delegations_status
    ON pending_delegations (status, created_at);
"""


@dataclass(frozen=True, slots=True)
class PendingDelegation:
    message_id: str
    correlation_id: str
    from_employee_id: str
    from_agent_id: str
    from_instance_id: str
    to_employee_id: str
    to_agent_id: str
    body: str
    issued_at: str
    status: str
    resolved_by: str | None
    resolved_at: str | None
    task_id: str | None
    conversation_id: str | None
    created_at: str


def _row_to_delegation(row: sqlite3.Row) -> PendingDelegation:
    return PendingDelegation(
        message_id=row["message_id"],
        correlation_id=row["correlation_id"],
        from_employee_id=row["from_employee_id"],
        from_agent_id=row["from_agent_id"],
        from_instance_id=row["from_instance_id"],
        to_employee_id=row["to_employee_id"],
        to_agent_id=row["to_agent_id"],
        body=row["body"],
        issued_at=row["issued_at"],
        status=row["status"],
        resolved_by=row["resolved_by"],
        resolved_at=row["resolved_at"],
        task_id=row["task_id"],
        conversation_id=row["conversation_id"],
        created_at=row["created_at"],
    )


class SqlitePendingDelegationRepository:
    """Persistencia de tarjetas de delegación entrante pendientes de HITL.

    Mantiene UNA conexión abierta durante la vida del objeto (en vez del
    patrón "conexión por llamada" de otros repos de este BC) porque
    `in_memory()` — usado por los tests — necesita que el esquema sobreviva
    entre llamadas: SQLite crea una base ':memory:' NUEVA y vacía por cada
    conexión, así que una conexión por-llamada perdería el esquema tras el
    primer `with`. `check_same_thread=False`: el D-Bus verb handler y el loop
    asyncio pueden invocar desde hilos distintos (mismo patrón que
    `SqliteAuthorizedTriggerRepository.in_memory`).
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        if str(db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_DDL_PENDING_DELEGATIONS)

    @classmethod
    def in_memory(cls) -> SqlitePendingDelegationRepository:
        """Instancia sobre ':memory:' — para tests, sin fichero de DB."""
        return cls(db_path=Path(":memory:"))

    def submit(self, *, envelope: dict[str, Any]) -> str:
        """Registra una DelegationEnvelope kind=request YA VERIFICADA.

        Idempotente (INSERT OR IGNORE por message_id): una re-entrega (p.ej.
        ack fallido en el tick anterior de config_sync) nunca duplica la
        tarjeta. Devuelve el status actual de la fila ('pending' en el caso
        normal, o el estado ya resuelto si esta re-entrega llega tras la
        decisión del humano).
        """
        now = datetime.now(tz=UTC).isoformat()
        conn = self._conn
        conn.execute(
            """
            INSERT OR IGNORE INTO pending_delegations (
                message_id, correlation_id, from_employee_id, from_agent_id,
                from_instance_id, to_employee_id, to_agent_id, body,
                issued_at, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                envelope["message_id"],
                envelope["correlation_id"],
                envelope["from_employee_id"],
                envelope.get("from_agent_id", ""),
                envelope["from_instance_id"],
                envelope.get("to_employee_id", ""),
                envelope.get("to_agent_id", ""),
                envelope["body"],
                envelope["issued_at"],
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT status FROM pending_delegations WHERE message_id = ?",
            (envelope["message_id"],),
        ).fetchone()
        return row["status"] if row is not None else "pending"

    def fetch(self, *, message_id: str) -> PendingDelegation | None:
        try:
            row = self._conn.execute(
                "SELECT * FROM pending_delegations WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        return _row_to_delegation(row) if row is not None else None

    def list_pending(self) -> list[PendingDelegation]:
        try:
            rows = self._conn.execute(
                "SELECT * FROM pending_delegations "
                "WHERE status = 'pending' ORDER BY created_at ASC"
            ).fetchall()
        except sqlite3.Error:
            return []
        return [_row_to_delegation(r) for r in rows]

    def resolve(
        self,
        *,
        message_id: str,
        status: str,
        resolved_by: str,
        task_id: str | None = None,
        conversation_id: str | None = None,
    ) -> bool:
        """Transición atómica 'pending' -> status (approved|rejected).

        True SOLO si esta llamada realizó la transición (WHERE status='pending')
        — un doble-clic/doble-verbo D-Bus sobre la MISMA tarjeta es un no-op
        (fail-closed: nunca se re-resuelve ni se re-encola una fila ya resuelta).
        """
        now = datetime.now(tz=UTC).isoformat()
        cursor = self._conn.execute(
            """
            UPDATE pending_delegations
               SET status = ?, resolved_by = ?, resolved_at = ?,
                   task_id = ?, conversation_id = ?
             WHERE message_id = ? AND status = 'pending'
            """,
            (status, resolved_by, now, task_id, conversation_id, message_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1
