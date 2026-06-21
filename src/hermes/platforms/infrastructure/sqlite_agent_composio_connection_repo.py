"""SqliteAgentComposioConnectionRepo — persistencia para AgentComposioConnection y alias.

Misma shell-state.db que el resto de repos daemon-owned (WAL, autocommit,
isolation_level=None). Dos tablas:
  - agent_composio_connections: binding agente↔cuenta-Composio (B2).
  - composio_connection_aliases: alias humano por cuenta-Composio (B3, global).

Sin PII. Solo IDs de dominio.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from hermes.capabilities.domain.agent_composio_connection import (
    AgentComposioConnection,
    BindingState,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_composio_connections (
  binding_id            TEXT PRIMARY KEY,
  tenant_id             TEXT NOT NULL,
  agent_id              TEXT NOT NULL,
  connected_account_id  TEXT NOT NULL,
  toolkit_slug          TEXT NOT NULL,
  bound_by              INTEGER NOT NULL,
  state                 TEXT NOT NULL DEFAULT 'bound',
  bound_at              TEXT NOT NULL,
  unbound_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_acc_agent
  ON agent_composio_connections (agent_id, tenant_id);
CREATE INDEX IF NOT EXISTS idx_acc_conn
  ON agent_composio_connections (connected_account_id);

CREATE TABLE IF NOT EXISTS composio_connection_aliases (
  connected_account_id  TEXT PRIMARY KEY,
  alias                 TEXT NOT NULL,
  set_by                INTEGER NOT NULL,
  updated_at            TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class SqliteAgentComposioConnectionRepo:
    """WAL autocommit SQLite repo para AgentComposioConnection + alias."""

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Bindings agente↔cuenta
    # ------------------------------------------------------------------

    def save(self, conn: AgentComposioConnection) -> None:
        """Upsert (INSERT OR REPLACE — idempotente por binding_id)."""
        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO agent_composio_connections
                  (binding_id, tenant_id, agent_id, connected_account_id,
                   toolkit_slug, bound_by, state, bound_at, unbound_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conn.binding_id,
                    conn.tenant_id,
                    conn.agent_id,
                    conn.connected_account_id,
                    conn.toolkit_slug,
                    conn.bound_by,
                    str(conn.state),
                    conn.bound_at.isoformat(),
                    conn.unbound_at.isoformat() if conn.unbound_at else None,
                ),
            )

    def find_active(
        self, agent_id: str, connected_account_id: str, tenant_id: str
    ) -> AgentComposioConnection | None:
        """Devuelve el binding activo para (agent, connection, tenant), o None."""
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM agent_composio_connections
                WHERE agent_id=? AND connected_account_id=?
                  AND tenant_id=? AND state='bound'
                ORDER BY bound_at DESC LIMIT 1
                """,
                (agent_id, connected_account_id, tenant_id),
            ).fetchone()
        return self._row_to_conn(row) if row else None

    def list_by_agent(
        self, agent_id: str, tenant_id: str
    ) -> list[AgentComposioConnection]:
        """Todos los bindings activos del agente en el tenant."""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM agent_composio_connections
                WHERE agent_id=? AND tenant_id=? AND state='bound'
                ORDER BY bound_at DESC
                """,
                (agent_id, tenant_id),
            ).fetchall()
        return [self._row_to_conn(r) for r in rows]

    def unbind(
        self, agent_id: str, connected_account_id: str, tenant_id: str
    ) -> bool:
        """Marca los bindings activos como unbound. Devuelve True si cambió algo."""
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE agent_composio_connections
                SET state='unbound', unbound_at=?
                WHERE agent_id=? AND connected_account_id=?
                  AND tenant_id=? AND state='bound'
                """,
                (_now_iso(), agent_id, connected_account_id, tenant_id),
            )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Alias humano por cuenta-Composio (global, no por agente)
    # ------------------------------------------------------------------

    def set_alias(
        self, connected_account_id: str, alias: str, set_by: int
    ) -> None:
        """Upsert alias para una cuenta Composio."""
        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO composio_connection_aliases
                  (connected_account_id, alias, set_by, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (connected_account_id, alias.strip(), set_by, _now_iso()),
            )

    def get_aliases(self) -> dict[str, str]:
        """Devuelve {connected_account_id: alias} para todas las cuentas con alias."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT connected_account_id, alias FROM composio_connection_aliases"
            ).fetchall()
        return {r["connected_account_id"]: r["alias"] for r in rows}

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_conn(row: sqlite3.Row) -> AgentComposioConnection:
        return AgentComposioConnection(
            binding_id=row["binding_id"],
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            connected_account_id=row["connected_account_id"],
            toolkit_slug=row["toolkit_slug"],
            bound_by=row["bound_by"],
            state=BindingState(row["state"]),
            bound_at=datetime.fromisoformat(row["bound_at"]),
            unbound_at=(
                datetime.fromisoformat(row["unbound_at"])
                if row["unbound_at"]
                else None
            ),
        )
