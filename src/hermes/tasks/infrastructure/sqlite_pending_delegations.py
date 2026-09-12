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

Una admisión se reclama de forma durable antes de encolar. Una excepción deja
la reclamación sin confirmar y bloquea replay, incluso después de reiniciar.
La lista propaga fallos: un almacén inaccesible no demuestra un buzón vacío.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_DDL_PENDING_DELEGATIONS = """
CREATE TABLE IF NOT EXISTS pending_delegations (
    message_id       TEXT PRIMARY KEY,
    correlation_id   TEXT NOT NULL,
    from_employee_id TEXT NOT NULL,
    from_agent_id    TEXT NOT NULL DEFAULT '',
    from_instance_id TEXT NOT NULL,
    to_employee_id   TEXT NOT NULL DEFAULT '',
    to_agent_id      TEXT NOT NULL DEFAULT '',
    to_instance_id   TEXT,
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
CREATE TABLE IF NOT EXISTS delegation_admission_claims (
    message_id TEXT PRIMARY KEY REFERENCES pending_delegations(message_id),
    claim_id TEXT NOT NULL UNIQUE,
    approved_by TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegation_admission_proofs (
    message_id TEXT PRIMARY KEY REFERENCES pending_delegations(message_id),
    proof_json TEXT NOT NULL
);
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
    to_instance_id: str | None = None
    admission_state: str | None = None


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
        to_instance_id=row["to_instance_id"],
        admission_state="unconfirmed"
        if dict(row).get("decision_claimed") and row["status"] == "pending" else None,
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
        # A verified recipient is required before sending execution telemetry.
        # Legacy rows remain unknown; never guess their destination after pairing.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(pending_delegations)")}
        if "to_instance_id" not in columns:
            try:
                self._conn.execute("ALTER TABLE pending_delegations ADD COLUMN to_instance_id TEXT")
            except sqlite3.OperationalError:
                columns = {row[1] for row in self._conn.execute("PRAGMA table_info(pending_delegations)")}
                if "to_instance_id" not in columns:
                    raise

    @classmethod
    def in_memory(cls) -> SqlitePendingDelegationRepository:
        """Instancia sobre ':memory:' — para tests, sin fichero de DB."""
        return cls(db_path=Path(":memory:"))

    def submit(self, *, envelope: dict[str, Any], proof: dict | None = None) -> str:
        """Registra una DelegationEnvelope kind=request YA VERIFICADA.

        Idempotente (INSERT OR IGNORE por message_id): una re-entrega (p.ej.
        ack fallido en el tick anterior de config_sync) nunca duplica la
        tarjeta. Devuelve el status actual de la fila ('pending' en el caso
        normal, o el estado ya resuelto si esta re-entrega llega tras la
        decisión del humano).
        """
        now = datetime.now(tz=UTC).isoformat()
        conn = self._conn
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO pending_delegations (
                message_id, correlation_id, from_employee_id, from_agent_id,
                from_instance_id, to_employee_id, to_agent_id, body,
                issued_at, status, created_at, to_instance_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
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
                envelope.get("to_instance_id"),
            ),
        )
        try:
            if inserted.rowcount == 1 and proof is not None:
                conn.execute(
                    "INSERT INTO delegation_admission_proofs (message_id, proof_json) VALUES (?, ?)",
                    (envelope["message_id"], json.dumps(proof, sort_keys=True)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        row = conn.execute(
            "SELECT status FROM pending_delegations WHERE message_id = ?",
            (envelope["message_id"],),
        ).fetchone()
        return row["status"] if row is not None else "pending"

    def admission_proof(self, *, message_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT proof_json FROM delegation_admission_proofs WHERE message_id=?", (message_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            proof = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return proof if isinstance(proof, dict) else None

    def fetch(self, *, message_id: str) -> PendingDelegation | None:
        try:
            row = self._conn.execute(
                "SELECT * FROM pending_delegations WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        return _row_to_delegation(row) if row is not None else None

    def claim_approval(
        self, *, message_id: str, approved_by: str, conversation_id: str
    ) -> str | None:
        """Commit admission intent before any queue/conversation side effect.

        A crash leaves an explicit uncertain claim, never permission to enqueue
        again. SQLite's single INSERT serializes competing processes as well as
        decisions in the daemon. No transaction is held across an await.
        """
        claim_id = str(uuid4())
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO delegation_admission_claims "
                "(message_id,claim_id,approved_by,conversation_id,claimed_at) "
                "SELECT message_id,?,?,?,? FROM pending_delegations "
                "WHERE message_id=? AND status='pending'",
                (claim_id, approved_by, conversation_id,
                 datetime.now(tz=UTC).isoformat(), message_id),
            )
        return claim_id if cursor.rowcount == 1 else None

    def release_unenqueued_claim(self, *, message_id: str, claim_id: str) -> None:
        """Only after an explicit gate denial before enqueue, never on exception."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM delegation_admission_claims WHERE message_id=? AND claim_id=?",
                (message_id, claim_id),
            )

    def list_pending(self) -> list[PendingDelegation]:
        rows = self._conn.execute(
            "SELECT d.*, EXISTS(SELECT 1 FROM delegation_admission_claims c "
            "WHERE c.message_id=d.message_id) AS decision_claimed "
            "FROM pending_delegations d "
            "WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
        return [_row_to_delegation(r) for r in rows]

    def resolve(
        self,
        *,
        message_id: str,
        status: str,
        resolved_by: str,
        task_id: str | None = None,
        conversation_id: str | None = None,
        claim_id: str | None = None,
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
               AND (NOT EXISTS (SELECT 1 FROM delegation_admission_claims c
                                WHERE c.message_id=pending_delegations.message_id)
                    OR (?='approved' AND EXISTS (
                        SELECT 1 FROM delegation_admission_claims c
                        WHERE c.message_id=pending_delegations.message_id
                          AND c.claim_id=? AND c.approved_by=? AND c.conversation_id=?)))
            """,
            (status, resolved_by, now, task_id, conversation_id, message_id,
             status, claim_id, resolved_by, conversation_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1
