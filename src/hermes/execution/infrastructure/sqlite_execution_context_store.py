"""SqliteExecutionContextStore — persistencia de `execution_contexts` (PIEZA 4).

Registro fail-closed de UN dueño por superficie de input/display
(data-model 006 §3.2 / FR-021..FR-023 / FR-026). Generaliza el
`InputOwnershipLedger` de teaching (in-memory) a un registro PERSISTENTE cuya
única razón de durabilidad es reconciliar dueños huérfanos tras un reinicio del
daemon (SC-010): el daemon usa `Restart=always` y puede reiniciarse en caliente
con tareas en curso.

Modelo híbrido (data-model 006 §3.2): la exclusión en caliente la da un lock
asyncio en memoria sobre el registry; ESTE store es la durabilidad + la red de
seguridad. El UNIQUE parcial `(input_surface, isolation_key) WHERE
status='claimed'` hace IMPOSIBLE a nivel de esquema dos dueños de la misma
superficie — defensa en profundidad ante un bug de la lógica en memoria.

Patrón replicado de `SQLiteConsentRepository` / `ensure_tasks_schema`:
conexión por llamada en autocommit (`isolation_level=None`), DDL idempotente
on-connect (`CREATE ... IF NOT EXISTS`), timestamps TEXT ISO-8601, UUIDs TEXT.
Re-ejecutar no destruye ni falla.

T063 extiende el store con:
  - write_claim: INSERT de una fila 'claimed' (write-through tras el lock en
    memoria).
  - write_release: UPDATE a 'released' (write-through al liberar).
  - reconcile_orphans: libera filas 'claimed' cuyo lease ha vencido (boot).
  - list_claimed: lectura para tests + monitoring.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# DDL FIRMADO — execution_contexts (data-model 006 §3.2 / §8 puntos 5-7).
_DDL_EXECUTION_CONTEXTS = """
CREATE TABLE IF NOT EXISTS execution_contexts (
    context_id      TEXT PRIMARY KEY,

    -- Superficie exclusiva poseída. Taxonomía alineada con SurfaceKind
    -- (data-model 006 §8 punto 6, FIRMADA).
    input_surface   TEXT NOT NULL
        CHECK (input_surface IN (
            'keyboard', 'mouse', 'display', 'browser',
            'terminal', 'filesystem', 'api_call',
            'desktop_app', 'system_settings', 'package_manager'
        )),

    -- Clave de aislamiento: una superficie lógica concreta. Para browser es la
    -- --session key (tenant:site); para keyboard/mouse/display es el seat /
    -- headless display id. Un dueño por (input_surface, isolation_key).
    isolation_key   TEXT NOT NULL,

    -- Dueño del canal de input (reusa InputOwner de teaching, §8 punto 7).
    input_owner     TEXT NOT NULL
        CHECK (input_owner IN ('agent', 'operator')),

    -- Qué tarea/worker posee el contexto (para liberar al terminar la tarea).
    owning_task_id  TEXT
        REFERENCES agent_tasks(task_id) ON DELETE SET NULL,
    owning_worker_id TEXT,

    -- Estado del lease (fail-safe: dueño muerto se recupera por expiración).
    status          TEXT NOT NULL DEFAULT 'claimed'
        CHECK (status IN ('claimed', 'released')),
    claimed_at      TEXT NOT NULL,
    lease_expires_at TEXT,
    released_at     TEXT,
    heartbeat_at    TEXT,

    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,

    -- I7 (P1): released => sin lease vivo (no bloquea re-claim).
    CHECK (status <> 'released' OR lease_expires_at IS NULL),
    -- I8 (P1): claimed => tiene dueño-worker y claimed_at (para reconciliar).
    CHECK (
        status <> 'claimed'
        OR (owning_worker_id IS NOT NULL AND claimed_at IS NOT NULL)
    )
);

-- INVARIANTE NÚCLEO (FR-021): a lo sumo UN dueño por superficie a la vez.
-- UNIQUE parcial => el segundo INSERT/UPDATE a 'claimed' sobre la misma
-- (input_surface, isolation_key) falla con UNIQUE violation => fail-closed
-- (FR-022). No comparten superficie.
CREATE UNIQUE INDEX IF NOT EXISTS execution_contexts_surface_owner_unique
    ON execution_contexts (input_surface, isolation_key)
    WHERE status = 'claimed';

-- Q: reconciliación de huérfanos (SC-010): 'claimed' con lease vencido tras
--    reinicio => liberar.
--    SELECT context_id WHERE status='claimed' AND lease_expires_at < now
CREATE INDEX IF NOT EXISTS idx_execution_contexts_lease
    ON execution_contexts (lease_expires_at)
    WHERE status = 'claimed';

-- Q: "qué contexto tiene esta tarea" (liberar al terminar la tarea).
CREATE INDEX IF NOT EXISTS idx_execution_contexts_task
    ON execution_contexts (owning_task_id)
    WHERE owning_task_id IS NOT NULL;
"""

# DDL for table without FK to agent_tasks — used when creating standalone store
# without agent_tasks present (e.g., isolated integration tests).
_DDL_EXECUTION_CONTEXTS_NO_FK = """
CREATE TABLE IF NOT EXISTS execution_contexts (
    context_id      TEXT PRIMARY KEY,

    input_surface   TEXT NOT NULL
        CHECK (input_surface IN (
            'keyboard', 'mouse', 'display', 'browser',
            'terminal', 'filesystem', 'api_call',
            'desktop_app', 'system_settings', 'package_manager'
        )),

    isolation_key   TEXT NOT NULL,

    input_owner     TEXT NOT NULL
        CHECK (input_owner IN ('agent', 'operator')),

    owning_task_id  TEXT,
    owning_worker_id TEXT,

    status          TEXT NOT NULL DEFAULT 'claimed'
        CHECK (status IN ('claimed', 'released')),
    claimed_at      TEXT NOT NULL,
    lease_expires_at TEXT,
    released_at     TEXT,
    heartbeat_at    TEXT,

    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,

    CHECK (status <> 'released' OR lease_expires_at IS NULL),
    CHECK (
        status <> 'claimed'
        OR (owning_worker_id IS NOT NULL AND claimed_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS execution_contexts_surface_owner_unique
    ON execution_contexts (input_surface, isolation_key)
    WHERE status = 'claimed';

CREATE INDEX IF NOT EXISTS idx_execution_contexts_lease
    ON execution_contexts (lease_expires_at)
    WHERE status = 'claimed';

CREATE INDEX IF NOT EXISTS idx_execution_contexts_task
    ON execution_contexts (owning_task_id)
    WHERE owning_task_id IS NOT NULL;
"""

_DEFAULT_LEASE_S = 300.0  # 5 minutos (workers renuevan via heartbeat)


def ensure_execution_contexts_schema(conn: sqlite3.Connection) -> None:
    """Aplica el DDL idempotente de `execution_contexts` sobre una conexión abierta.

    `CREATE TABLE/INDEX IF NOT EXISTS`. Forward-only; re-ejecutar no destruye ni
    lanza. La FK a `agent_tasks(task_id)` exige que `ensure_tasks_schema` se haya
    aplicado antes sobre la misma DB (mismo fichero `shell-state.db`).
    """
    conn.executescript(_DDL_EXECUTION_CONTEXTS)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _future_iso(seconds: float) -> str:
    from datetime import timedelta  # noqa: PLC0415

    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


class SqliteExecutionContextStore:
    """Almacén SQLite del registro de contextos de ejecución (PIEZA 4).

    Conexión por llamada en autocommit; DDL idempotente on-connect. La exclusión
    en caliente la resuelve el registry en memoria (lock asyncio); este store
    aporta durabilidad + el UNIQUE parcial como red de seguridad.

    T063: añade write_claim / write_release / reconcile_orphans / list_claimed.
    """

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        # FK enabled only when agent_tasks exists; standalone tests skip FK.
        conn.execute("PRAGMA foreign_keys = OFF")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            # Check if agent_tasks exists to decide which DDL to use.
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "agent_tasks" in tables:
                conn.executescript(_DDL_EXECUTION_CONTEXTS)
            else:
                conn.executescript(_DDL_EXECUTION_CONTEXTS_NO_FK)

    # ------------------------------------------------------------------
    # T063: write-through operations (called by ExecutionContextRegistry)
    # ------------------------------------------------------------------

    def write_claim(
        self,
        *,
        context_id: str,
        input_surface: str,
        isolation_key: str,
        input_owner: str,
        owning_worker_id: str,
        lease_seconds: float = _DEFAULT_LEASE_S,
    ) -> None:
        """Persist a new 'claimed' row (write-through after the in-memory lock).

        Uses INSERT OR IGNORE so that re-claiming by the same logical owner
        (idempotent path) does not raise at the DB level — the in-memory lock
        already handles idempotency semantics. The UNIQUE partial index is the
        last line of defense against concurrent bugs.
        """
        now = _now_iso()
        lease_exp = _future_iso(lease_seconds) if lease_seconds > 0 else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO execution_contexts (
                    context_id, input_surface, isolation_key,
                    input_owner, owning_worker_id,
                    status, claimed_at, lease_expires_at,
                    created_at, updated_at
                ) VALUES (
                    :ctx, :surface, :key,
                    :owner_kind, :worker_id,
                    'claimed', :now, :lease,
                    :now, :now
                )
                """,
                {
                    "ctx": context_id,
                    "surface": input_surface,
                    "key": isolation_key,
                    "owner_kind": input_owner,
                    "worker_id": owning_worker_id,
                    "now": now,
                    "lease": lease_exp,
                },
            )

    def write_release(self, *, context_id: str) -> None:
        """Mark a context as 'released' (write-through after the in-memory release).

        No-op if the row does not exist or is already 'released' (safe for
        double-release / cleanup paths, FR-023).
        """
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE execution_contexts
                SET status='released', released_at=:now,
                    lease_expires_at=NULL, updated_at=:now
                WHERE context_id=:ctx AND status='claimed'
                """,
                {"now": now, "ctx": context_id},
            )

    # ------------------------------------------------------------------
    # T063: reconcile_orphans — boot-time cleanup (SC-010, CTRL-P1-20)
    # ------------------------------------------------------------------

    def reconcile_orphans(self) -> int:
        """Release ALL 'claimed' rows — daemon bootstrap cleanup (FR-026, SC-010).

        At boot time, every 'claimed' row is an orphan: the process that held
        the lease has died. There is no way to know which claims had live
        processes, so ALL are released unconditionally (fail-safe default-deny).

        The lease field's purpose is for within-process heartbeat monitoring
        (detecting zombie workers). At cross-restart boundaries, only process
        presence matters — and after a restart, no previous process is present.

        Returns the number of rows released.
        """
        now = _now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE execution_contexts
                SET status='released', released_at=:now,
                    lease_expires_at=NULL, updated_at=:now
                WHERE status='claimed'
                """,
                {"now": now},
            )
            return cursor.rowcount

    def reconcile_all_claimed(self) -> int:
        """Release ALL 'claimed' rows regardless of lease expiry.

        Used by ExecutionContextRegistry.reconcile() which models a full
        in-memory wipe on daemon restart (FR-026): after restart the in-memory
        state is gone, so ALL persisted claims are orphaned from the new
        process's perspective.

        Returns the number of rows released.
        """
        now = _now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE execution_contexts
                SET status='released', released_at=:now,
                    lease_expires_at=NULL, updated_at=:now
                WHERE status='claimed'
                """,
                {"now": now},
            )
            return cursor.rowcount

    # ------------------------------------------------------------------
    # T063: read helpers (monitoring + tests)
    # ------------------------------------------------------------------

    def list_claimed(self) -> list[dict[str, Any]]:
        """Return all currently 'claimed' rows as plain dicts.

        Used by tests and by the health-monitoring layer; never exposes raw
        sqlite3.Row objects outside this store.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_contexts WHERE status='claimed'"
            ).fetchall()
            return [dict(r) for r in rows]
