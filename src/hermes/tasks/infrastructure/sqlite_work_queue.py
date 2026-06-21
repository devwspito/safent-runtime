"""SqliteWorkQueue — implementación durable de WorkQueuePort sobre SQLite WAL.

Dequeue atómico: BEGIN IMMEDIATE + UPDATE WHERE status='pending' AND available_at<=now
ORDER BY priority DESC, created_at ASC LIMIT 1 RETURNING. Single-writer garantizado
por WAL + IMMEDIATE lock. Sin doble-toma.

CTRL-10: enqueue rechaza si payload no incluye 'enqueued_by'.
CTRL-16: mark_failed respeta max_retries del item (tope en dominio + CHECK I4 en BD).

Patrón: conexión por llamada, autocommit (isolation_level=None), row_factory sqlite3.Row.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from hermes.tasks.domain import work_item as _domain
from hermes.tasks.domain.ports import TaskStatus, WorkItem, WorkItemKind, WorkQueuePort
from hermes.tasks.infrastructure.schema import ensure_tasks_schema

# Lease por defecto en segundos — debe exceder cómodamente un ciclo LLM + API calls.
# Con N workers concurrentes, reconcile_stale() re-encola tareas cuyo lease expiró,
# lo que provocaría ejecución duplicada si el lease es más corto que la tarea real.
# 600 s (10 min) es un margen seguro para Phase 1; Phase 2 introducirá
# lease-renewal/heartbeat por worker para no depender de un valor fijo.
_LEASE_SECONDS: int = int(os.environ.get("HERMES_TASK_LEASE_SECONDS", "600"))
# Backoff base en segundos (consistent con work_item.py)
_BACKOFF_BASE_SECONDS: int = 30
_BACKOFF_CAP_SECONDS: int = 3600

_TERMINAL_STATUSES = frozenset({"completed", "failed", "rejected"})

# Identificador por defecto del worker que reclama (data-model 006 §A5). El loop
# P0 es single-writer; el id es estable dentro de un arranque del daemon. Satisface
# la invariante I6 ('in_progress' => worker_id NOT NULL) del esquema P1.
_DEFAULT_WORKER_ID: str = "worker-0"


class MissingEnqueuedBy(ValueError):
    """CTRL-10: el item no incluye enqueued_by en su payload — rechazado fail-closed."""


class ClaimTokenMismatch(ValueError):
    """I7: el claim_token no coincide con el del item — transición rechazada."""


class SqliteWorkQueue:
    """Cola durable sobre SQLite WAL. Implementa WorkQueuePort."""

    def __init__(self, *, db_path: Path, worker_id: str = _DEFAULT_WORKER_ID) -> None:
        self._db_path = db_path
        self._worker_id = worker_id
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # WorkQueuePort
    # ------------------------------------------------------------------

    async def enqueue(self, item: WorkItem) -> WorkItem:
        """Inserta PENDING. Idempotente por dedup_key. Rechaza sin enqueued_by (CTRL-10)."""
        _assert_enqueued_by(item)

        if item.dedup_key is not None:
            existing = await self.find_by_dedup_key(item.dedup_key)
            if existing is not None:
                return existing

        now_iso = _iso(datetime.now(tz=UTC))
        # I5 (data-model 006): chat_message ⇒ conversation_id NOT NULL.
        # conversation_id viaja en el payload; lo espejamos en la columna
        # dedicada para que el índice idx_agent_tasks_conversation funcione
        # y la invariante I5 del esquema SQLite quede satisfecha.
        conversation_id = item.payload.get("conversation_id") or None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_tasks (
                    task_id, trigger_kind, enqueued_by, operator_id,
                    instruction, payload_json, tenant_id, dedup_key,
                    priority, status, max_retries, kind, conversation_id,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, 'pending', ?, ?, ?,
                    ?, ?
                )
                """,
                (
                    str(item.id),
                    item.trigger_kind,
                    item.payload.get("enqueued_by", ""),
                    str(item.tenant_id),
                    item.payload.get("instruction", ""),
                    json.dumps(item.payload),
                    str(item.tenant_id),
                    item.dedup_key,
                    item.priority,
                    item.max_attempts,
                    str(item.kind),
                    conversation_id,
                    now_iso,
                    now_iso,
                ),
            )
        return item

    async def claim_next(self) -> WorkItem | None:
        """Dequeue atómico via BEGIN IMMEDIATE. Única función que escribe IN_PROGRESS."""
        now = datetime.now(tz=UTC)
        now_iso = _iso(now)
        claim_token = str(uuid4())
        lease_expires_iso = _iso(now + timedelta(seconds=_LEASE_SECONDS))

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT task_id FROM agent_tasks
                    WHERE status = 'pending'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                    """,
                    (now_iso,),
                ).fetchone()

                if row is None:
                    conn.execute("ROLLBACK")
                    return None

                task_id = row["task_id"]
                conn.execute(
                    """
                    UPDATE agent_tasks
                    SET status = 'in_progress',
                        retry_count = retry_count + 1,
                        claim_token = ?,
                        claimed_at  = ?,
                        lease_expires_at = ?,
                        worker_id   = ?,
                        updated_at  = ?
                    WHERE task_id = ? AND status = 'pending'
                    """,
                    (
                        claim_token,
                        now_iso,
                        lease_expires_iso,
                        self._worker_id,
                        now_iso,
                        task_id,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return await self._load_item(task_id)

    async def mark_completed(
        self,
        item_id: UUID,
        *,
        claim_token: UUID,
        audit_entry_id: UUID,
        execution_head_hash: str | None = None,
    ) -> None:
        """COMPLETED con evidencia real. CHECK I1 en la BD garantiza la invariante.

        Args:
            execution_head_hash: signed_payload_hash_hex del audit de ejecución
                (propagado desde ExecutionOutcome.execution_head_hash). Si None,
                persiste cadena vacía como indicador de ausencia.
        """
        now_iso = _iso(datetime.now(tz=UTC))
        head_hash = execution_head_hash or ""

        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_tasks
                SET status = 'completed',
                    claim_token      = NULL,
                    claimed_at       = NULL,
                    lease_expires_at = NULL,
                    execution_audit_entry_id = ?,
                    execution_head_hash      = ?,
                    updated_at       = ?
                WHERE task_id = ?
                  AND status = 'in_progress'
                  AND claim_token = ?
                """,
                (
                    str(audit_entry_id),
                    head_hash,
                    now_iso,
                    str(item_id),
                    str(claim_token),
                ),
            )
        if cursor.rowcount == 0:
            raise ClaimTokenMismatch(
                f"mark_completed: item {item_id} no encontrado, "
                "no está in_progress, o claim_token incorrecto"
            )

    async def mark_failed(
        self, item_id: UUID, *, claim_token: UUID, reason: str
    ) -> WorkItem:
        """FAILED con backoff — delega transición en la máquina de estados de dominio.

        Usa work_item.mark_failed como fuente única de verdad para la lógica de
        reintento y backoff (I5). El SQL persiste el resultado sin reimplementarlo.
        """
        item = await self._load_item(str(item_id))
        if item is None:
            raise ValueError(f"WorkItem {item_id} no encontrado")

        try:
            next_state = _domain.mark_failed(item, claim_token=claim_token, reason=reason)
        except _domain.IllegalTransition as exc:
            raise ClaimTokenMismatch(str(exc)) from exc

        now_iso = _iso(datetime.now(tz=UTC))

        if next_state.status is TaskStatus.PENDING:
            # Reintento con backoff: next_state.available_at ya lo calculó el dominio.
            next_attempt_iso = _iso(next_state.available_at)
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE agent_tasks
                    SET status           = 'pending',
                        claim_token      = NULL,
                        claimed_at       = NULL,
                        lease_expires_at = NULL,
                        last_error       = ?,
                        next_attempt_at  = ?,
                        updated_at       = ?
                    WHERE task_id = ?
                      AND status = 'in_progress'
                      AND claim_token = ?
                    """,
                    (reason, next_attempt_iso, now_iso, str(item_id), str(claim_token)),
                )
        else:
            # Terminal FAILED.
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE agent_tasks
                    SET status           = 'failed',
                        claim_token      = NULL,
                        claimed_at       = NULL,
                        lease_expires_at = NULL,
                        last_error       = ?,
                        updated_at       = ?
                    WHERE task_id = ?
                      AND status = 'in_progress'
                      AND claim_token = ?
                    """,
                    (reason, now_iso, str(item_id), str(claim_token)),
                )

        updated = await self._load_item(str(item_id))
        assert updated is not None
        return updated

    async def mark_pending_approval(
        self, item_id: UUID, *, claim_token: UUID, proposal_id: UUID
    ) -> None:
        """PENDING_APPROVAL — libera lease (LOOP-4).

        Persiste _pending_proposal_id en payload_json para que el loop pueda
        recuperar el token aprobado al re-encolar tras aprobación (FR-015).
        """
        now_iso = _iso(datetime.now(tz=UTC))
        with self._connect() as conn:
            # Leer payload_json actual para inyectar _pending_proposal_id
            row = conn.execute(
                "SELECT payload_json FROM agent_tasks WHERE task_id = ?",
                (str(item_id),),
            ).fetchone()
            if row is not None:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                payload["_pending_proposal_id"] = str(proposal_id)
                payload_json = json.dumps(payload)
            else:
                payload_json = json.dumps({"_pending_proposal_id": str(proposal_id)})

            cursor = conn.execute(
                """
                UPDATE agent_tasks
                SET status           = 'pending_approval',
                    claim_token      = NULL,
                    claimed_at       = NULL,
                    lease_expires_at = NULL,
                    payload_json     = ?,
                    updated_at       = ?
                WHERE task_id = ?
                  AND status = 'in_progress'
                  AND claim_token = ?
                """,
                (payload_json, now_iso, str(item_id), str(claim_token)),
            )
        if cursor.rowcount == 0:
            raise ClaimTokenMismatch(
                f"mark_pending_approval: item {item_id} no encontrado "
                "o claim_token incorrecto"
            )

    async def mark_rejected(
        self, item_id: UUID, *, claim_token: UUID, reason: str
    ) -> None:
        """REJECTED terminal."""
        now_iso = _iso(datetime.now(tz=UTC))
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_tasks
                SET status           = 'rejected',
                    claim_token      = NULL,
                    claimed_at       = NULL,
                    lease_expires_at = NULL,
                    last_error       = ?,
                    updated_at       = ?
                WHERE task_id = ?
                  AND status = 'in_progress'
                  AND claim_token = ?
                """,
                (reason, now_iso, str(item_id), str(claim_token)),
            )
        if cursor.rowcount == 0:
            raise ClaimTokenMismatch(
                f"mark_rejected: item {item_id} no encontrado "
                "o claim_token incorrecto"
            )

    async def reconcile_stale(self) -> int:
        """Re-encola IN_PROGRESS con lease vencido (FR-007/SC-003)."""
        now_iso = _iso(datetime.now(tz=UTC))
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_tasks
                SET status           = 'pending',
                    claim_token      = NULL,
                    claimed_at       = NULL,
                    lease_expires_at = NULL,
                    updated_at       = ?
                WHERE status = 'in_progress'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < ?
                """,
                (now_iso, now_iso),
            )
        return cursor.rowcount

    async def find_by_dedup_key(self, dedup_key: str) -> WorkItem | None:
        """Busca item VIVO (no terminal) por dedup_key (SC-007)."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT task_id FROM agent_tasks
                WHERE dedup_key = ?
                  AND status NOT IN ('completed','failed','rejected')
                LIMIT 1
                """,
                (dedup_key,),
            ).fetchone()
        if row is None:
            return None
        return await self._load_item(row["task_id"])

    async def renew_lease(self, item_id: UUID, *, claim_token: UUID) -> bool:
        """Renueva lease si el claim_token coincide y el item sigue in_progress.

        Returns True si se actualizó exactamente una fila (lease renovado).
        Returns False si el item ya no pertenece a este worker — el worker
        debe dejar de procesar para evitar efectos duplicados.

        Idempotente: renovar dos veces con el mismo token es seguro (ambas
        devuelven True, sólo el timestamp cambia).
        """
        now = datetime.now(tz=UTC)
        new_lease_iso = _iso(now + timedelta(seconds=_LEASE_SECONDS))
        now_iso = _iso(now)

        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_tasks
                SET lease_expires_at = ?,
                    updated_at       = ?
                WHERE task_id    = ?
                  AND status     = 'in_progress'
                  AND claim_token = ?
                """,
                (new_lease_iso, now_iso, str(item_id), str(claim_token)),
            )
        return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Lectura de producción (CTRL-P1-5 — solo metadatos al caller)
    # ------------------------------------------------------------------

    async def list_by_status(
        self, *, status: TaskStatus, limit: int = 50
    ) -> list[WorkItem]:
        """Items por status, prioridad desc, created_at asc. Blocker de oleada 2.

        CTRL-P1-5: el caller (ControlPlaneService) expone SOLO metadatos al exterior.
        Esta función devuelve WorkItem completo; la filtración de payload la hace
        el service layer (PendingTaskView/TaskStatusView — nunca payload_json).
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT task_id FROM agent_tasks
                WHERE status = ?
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """,
                (str(status), limit),
            ).fetchall()
        items = []
        for row in rows:
            item = await self._load_item(row["task_id"])
            if item is not None:
                items.append(item)
        return items

    async def task_by_id(self, *, task_id: UUID) -> WorkItem | None:
        """Carga un WorkItem por task_id. None si no existe. Blocker de oleada 2.

        CTRL-P1-5: el caller (ControlPlaneService) expone SOLO metadatos al exterior.
        """
        return await self._load_item(str(task_id))

    async def re_enqueue_after_approval(self, item_id: UUID) -> None:
        """PENDING_APPROVAL -> PENDING tras aprobación humana (FR-015).

        El operador aprobó la propuesta; el broker al re-dispatchar la encontrará
        con token válido en la ApprovalGate. La tarea vuelve a la cola para ser
        reclamada por el loop en el próximo ciclo.

        Raises:
            ValueError: si el item no existe o no está en PENDING_APPROVAL.
        """
        now_iso = _iso(datetime.now(tz=UTC))
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_tasks
                SET status      = 'pending',
                    updated_at  = ?
                WHERE task_id   = ?
                  AND status    = 'pending_approval'
                """,
                (now_iso, str(item_id)),
            )
        if cursor.rowcount == 0:
            raise ValueError(
                f"re_enqueue_after_approval: item {item_id} no existe "
                "o no está en estado 'pending_approval'"
            )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        # F-08: bajo N workers, un BEGIN IMMEDIATE contendido debe ESPERAR el lock
        # en vez de lanzar "database is locked" al instante (lo que mataría al
        # worker). busy_timeout serializa la contención con espera acotada.
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            ensure_tasks_schema(conn)

    async def _load_item(self, task_id: str) -> WorkItem | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return _row_to_work_item(row)


# Satisface WorkQueuePort structural check
assert isinstance(SqliteWorkQueue.__new__(SqliteWorkQueue), WorkQueuePort)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _uuid_safe(val: str | None) -> UUID | None:
    """Parse UUID string, returning None if invalid or missing."""
    if not val:
        return None
    try:
        return UUID(val)
    except (ValueError, AttributeError):
        return None


def _assert_enqueued_by(item: WorkItem) -> None:
    """CTRL-10: exige enqueued_by en payload."""
    if not item.payload.get("enqueued_by"):
        raise MissingEnqueuedBy(
            f"WorkItem {item.id}: 'enqueued_by' requerido en payload (CTRL-10). "
            "Incluye el ID del origen que encola para trazabilidad."
        )


def _row_to_work_item(row: sqlite3.Row) -> WorkItem:
    """Mapea una fila de agent_tasks a WorkItem de dominio."""

    def _dt(val: str | None) -> datetime | None:
        return datetime.fromisoformat(val) if val else None

    payload: dict = {}
    raw_payload = row["payload_json"]
    if raw_payload:
        try:
            payload = json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            payload = {}
    # La instrucción se persiste en su COLUMNA dedicada (canónica) además de en
    # payload_json. Sembramos payload["instruction"] desde la columna para que el
    # texto del operador SIEMPRE llegue al engine (build_decision_context →
    # operator_instruction), aunque payload_json no la traiga.
    _instr_col = row["instruction"]
    if _instr_col and not payload.get("instruction"):
        payload["instruction"] = _instr_col

    status_raw = row["status"]
    status = TaskStatus(status_raw)

    claim_token_uuid = _uuid_safe(row["claim_token"])
    tenant_id = _uuid_safe(row["tenant_id"]) or _uuid_safe(row["operator_id"])
    if tenant_id is None:
        # Fallback: derive a deterministic UUID from the raw string (test data)
        import hashlib  # noqa: PLC0415
        raw = str(row["tenant_id"] or row["operator_id"] or "unknown")
        digest = hashlib.sha256(raw.encode()).digest()
        tenant_id = UUID(bytes=digest[:16])

    # Reconstruct kind from the persisted column (never use default).
    # The column was added in migration P1; rows from P0 have DEFAULT 'autonomous'.
    kind_raw = row["kind"] if row["kind"] else "autonomous"
    try:
        item_kind = WorkItemKind(kind_raw)
    except ValueError:
        item_kind = WorkItemKind.AUTONOMOUS

    return WorkItem(
        id=UUID(row["task_id"]),
        tenant_id=tenant_id,
        trigger_kind=row["trigger_kind"],
        kind=item_kind,
        priority=row["priority"] or 0,
        dedup_key=row["dedup_key"],
        status=status,
        subjects=(),
        constraints={},
        payload=payload,
        attempts=row["retry_count"] or 0,
        max_attempts=row["max_retries"] or 3,
        claim_token=claim_token_uuid,
        available_at=_dt(row["next_attempt_at"]) or datetime.now(tz=UTC),
        claimed_at=_dt(row["claimed_at"]),
        lease_expires_at=_dt(row["lease_expires_at"]),
        enqueued_at=_dt(row["created_at"]) or datetime.now(tz=UTC),
    )
