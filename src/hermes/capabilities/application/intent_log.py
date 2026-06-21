"""T038 — IntentLog (CTRL-11/RECON-1/TOP-7).

Outbox idempotente para prevenir re-ejecución de efectos tras reinicio.

Protocolo:
  1. `record_intent(key, proposal)` ANTES del efecto (salida al SO).
  2. `record_outcome(key, outcome)` DESPUÉS del efecto (resultado real).
  3. `was_executed(key)` → True si hay outcome con status=EXECUTED.
  4. `pending_intents()` → lista de keys con intent pero sin outcome.
     Un intent pendiente NO se re-ejecuta a ciegas (needs_human_review).

idempotency_key = SHA-256 de la serialización estable del proposal:
  sha256(json({proposal_id, tool_name, tenant_id, parameters}, sort_keys=True))

Persistencia:
  - Sin `db_path`: almacén in-memory (unit tests, sin I/O).
  - Con `db_path`: SQLite con DDL idempotente on-connect, patrón del consent
    repo. Marcado `@pytest.mark.integration` en los tests que lo usen.

Separación de capas:
  - `IntentLog` es application. Sin framework.
  - `_SqliteIntentStore` es la infraestructura interna. Solo SQLite stdlib.
  - `_InMemoryIntentStore` para tests sin disco.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from abc import ABC, abstractmethod

from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
from hermes.domain.proposal import ToolCallProposal

# ---------------------------------------------------------------------------
# idempotency_key helper (pública para que los tests repliquen el mismo hash)
# ---------------------------------------------------------------------------


def compute_idempotency_key(proposal: ToolCallProposal) -> str:
    """SHA-256 de la serialización estable del proposal.

    Incluye: proposal_id, tool_name, tenant_id, parameters.
    Excluye: justification (narrativa del LLM, no forma parte del efecto).
    """
    stable = json.dumps(
        {
            "proposal_id": str(proposal.proposal_id),
            "tool_name": proposal.tool_name,
            "tenant_id": str(proposal.tenant_id),
            "parameters": proposal.parameters,
        },
        sort_keys=True,
    ).encode()
    return hashlib.sha256(stable).hexdigest()


# ---------------------------------------------------------------------------
# Store interface (puerto interno de infraestructura)
# ---------------------------------------------------------------------------


class _IntentStorePort(ABC):
    @abstractmethod
    def save_intent(self, key: str, proposal_json: str, task_id: str | None = None) -> None: ...

    @abstractmethod
    def save_outcome(self, key: str, status: str) -> None: ...

    @abstractmethod
    def get_status(self, key: str) -> str | None: ...

    @abstractmethod
    def has_intent_without_outcome(self, key: str) -> bool:
        """True si hay un intent registrado para esta key pero sin outcome."""
        ...

    @abstractmethod
    def pending_keys(self) -> list[str]: ...

    @abstractmethod
    def pending_task_ids(self) -> list[str]:
        """IDs de tarea con intent registrado pero sin outcome (RECON-1)."""
        ...


# ---------------------------------------------------------------------------
# In-memory store (unit tests)
# ---------------------------------------------------------------------------


class _InMemoryIntentStore(_IntentStorePort):
    def __init__(self) -> None:
        self._intents: dict[str, str] = {}            # key -> proposal_json
        self._task_ids: dict[str, str | None] = {}    # key -> task_id
        self._outcomes: dict[str, str] = {}           # key -> status

    def save_intent(self, key: str, proposal_json: str, task_id: str | None = None) -> None:
        self._intents.setdefault(key, proposal_json)  # idempotente
        if task_id is not None:
            self._task_ids.setdefault(key, task_id)

    def save_outcome(self, key: str, status: str) -> None:
        self._outcomes[key] = status

    def get_status(self, key: str) -> str | None:
        return self._outcomes.get(key)

    def has_intent_without_outcome(self, key: str) -> bool:
        return key in self._intents and key not in self._outcomes

    def pending_keys(self) -> list[str]:
        return [k for k in self._intents if k not in self._outcomes]

    def pending_task_ids(self) -> list[str]:
        return [
            tid
            for k in self._intents
            if k not in self._outcomes
            if (tid := self._task_ids.get(k)) is not None
        ]


# ---------------------------------------------------------------------------
# SQLite store (integration tests + production)
# ---------------------------------------------------------------------------

_DDL_INTENT_LOG = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS intent_log (
    idempotency_key  TEXT PRIMARY KEY,
    proposal_json    TEXT NOT NULL,
    task_id          TEXT,
    recorded_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE TABLE IF NOT EXISTS intent_outcomes (
    idempotency_key  TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    recorded_at      TEXT NOT NULL DEFAULT (datetime('now','utc')),
    FOREIGN KEY (idempotency_key) REFERENCES intent_log (idempotency_key)
);
"""

# Migración idempotente para DBs existentes que no tienen la columna task_id.
_DDL_ADD_TASK_ID_COLUMN = "ALTER TABLE intent_log ADD COLUMN task_id TEXT"


class _SqliteIntentStore(_IntentStorePort):
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.executescript(_DDL_INTENT_LOG)
        conn.commit()
        # Migración idempotente: añadir task_id si no existe (DBs antiguas).
        try:
            conn.execute(_DDL_ADD_TASK_ID_COLUMN)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # La columna ya existe — no es un error.
        return conn

    def save_intent(self, key: str, proposal_json: str, task_id: str | None = None) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO intent_log (idempotency_key, proposal_json, task_id)"
            " VALUES (?, ?, ?)",
            (key, proposal_json, task_id),
        )
        self._conn.commit()

    def save_outcome(self, key: str, status: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO intent_outcomes (idempotency_key, status)"
            " VALUES (?, ?)",
            (key, status),
        )
        self._conn.commit()

    def get_status(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM intent_outcomes WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def has_intent_without_outcome(self, key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM intent_log il"
            " LEFT JOIN intent_outcomes io USING (idempotency_key)"
            " WHERE il.idempotency_key = ? AND io.idempotency_key IS NULL",
            (key,),
        ).fetchone()
        return row is not None

    def pending_keys(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT il.idempotency_key"
            " FROM intent_log il"
            " LEFT JOIN intent_outcomes io USING (idempotency_key)"
            " WHERE io.idempotency_key IS NULL"
        ).fetchall()
        return [r[0] for r in rows]

    def pending_task_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT il.task_id"
            " FROM intent_log il"
            " LEFT JOIN intent_outcomes io USING (idempotency_key)"
            " WHERE io.idempotency_key IS NULL AND il.task_id IS NOT NULL"
        ).fetchall()
        return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# IntentLog — API pública (application layer)
# ---------------------------------------------------------------------------


class IntentLog:
    """Outbox idempotente: registra intents y outcomes para reconciliación.

    Args:
        db_path: ruta al SQLite shell-state.db. Si es None, usa almacén
                 in-memory (apto para unit tests sin I/O).
    """

    def __init__(self, *, db_path: str | None = None) -> None:
        self._store: _IntentStorePort = (
            _SqliteIntentStore(db_path) if db_path else _InMemoryIntentStore()
        )

    def record_intent(
        self,
        idempotency_key: str,
        proposal: ToolCallProposal,
        *,
        task_id: str | None = None,
    ) -> None:
        """Registra la intención de ejecutar la propuesta ANTES del efecto.

        Idempotente: si la key ya existe, no sobreescribe.
        task_id: ID de la tarea de cola que generó este intent. Permite a
            bootstrap() marcar la tarea como needs_human_review si hay un
            intent pendiente sin outcome (crash entre record_intent y record_outcome).
        """
        proposal_json = _serialize_proposal(proposal)
        self._store.save_intent(idempotency_key, proposal_json, task_id=task_id)

    def record_outcome(self, idempotency_key: str, outcome: ExecutionOutcome) -> None:
        """Registra el resultado real DESPUÉS del efecto."""
        self._store.save_outcome(idempotency_key, outcome.status.value)

    def was_executed(self, idempotency_key: str) -> bool:
        """True si hay un outcome con status=EXECUTED para esta key."""
        status = self._store.get_status(idempotency_key)
        return status == ExecutionStatus.EXECUTED.value

    def has_pending_intent(self, idempotency_key: str) -> bool:
        """True si hay un intent sin outcome (crash entre record_intent y record_outcome).

        CTRL-11/RECON-1: el efecto puede haberse ejecutado o no. Para garantizar
        exactamente-una-vez, el broker NO debe re-ejecutar — devuelve needs_human_review.
        """
        return self._store.has_intent_without_outcome(idempotency_key)

    def pending_intents(self) -> list[str]:
        """Keys con intent registrado pero sin outcome.

        Un intent pendiente indica que el proceso crasheó entre el
        record_intent y el record_outcome. NO re-ejecutar a ciegas —
        el operador debe revisar (needs_human_review, RECON-1).
        """
        return self._store.pending_keys()

    def pending_task_ids(self) -> list[str]:
        """IDs de tarea con al menos un intent pendiente sin outcome (RECON-1).

        Usados por AgentLoopOrchestrator.bootstrap() para marcar las tareas
        como FAILED (needs_human_review) en vez de re-ejecutarlas a ciegas.
        """
        return self._store.pending_task_ids()


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------


def _serialize_proposal(proposal: ToolCallProposal) -> str:
    """Serialización estable del proposal para persistencia."""
    return json.dumps(
        {
            "proposal_id": str(proposal.proposal_id),
            "tool_name": proposal.tool_name,
            "tenant_id": str(proposal.tenant_id),
            "entity_id": proposal.entity_id,
            "entity_type": proposal.entity_type,
            "parameters": proposal.parameters,
            "justification": proposal.justification,
        },
        sort_keys=True,
    )
