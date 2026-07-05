"""SqliteAgentAccessScopeRepo — persistence for AgentAccessScope.

Enterprise governance, Fase 2 Phase 1 (runtime-only; no cloud/config-sync in
this phase). Table `agent_access_scopes` lives in the SAME shell-state.db as
`pending_approvals` (schema.py / ensure_capabilities_schema) — connection per
call, WAL autocommit, same pattern as SqliteApprovalGate. One row per
(tenant_id, agent_id): upsert is keyed on that composite pair (not scope_id),
so re-saving an agent's scope always replaces its single row instead of
accumulating history.

Capa: infrastructure. Maps DB rows <-> the pure AgentAccessScope aggregate;
no ORM, no framework leakage into the domain.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from hermes.capabilities.domain.agent_access_scope import AgentAccessScope
from hermes.capabilities.infrastructure.schema import ensure_capabilities_schema


class SqliteAgentAccessScopeRepo:
    """WAL SQLite repository for AgentAccessScope, one row per (tenant_id, agent_id)."""

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            ensure_capabilities_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL: the daemon executor thread (writes) and D-Bus/admin threads
        # (reads/writes) may operate concurrently — same rationale as
        # SqliteApprovalGate. busy_timeout bounds the wait under contention.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def upsert(self, scope: AgentAccessScope) -> None:
        """Insert or replace the (tenant_id, agent_id) row for *scope*."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_access_scopes (
                    tenant_id, agent_id, scope_id, native_tools, policy_overlay,
                    views, cerebro_unrestricted, enforced, updated_by, managed_by,
                    approval_tier, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, agent_id) DO UPDATE SET
                    scope_id             = excluded.scope_id,
                    native_tools         = excluded.native_tools,
                    policy_overlay       = excluded.policy_overlay,
                    views                = excluded.views,
                    cerebro_unrestricted = excluded.cerebro_unrestricted,
                    enforced             = excluded.enforced,
                    updated_by           = excluded.updated_by,
                    managed_by           = excluded.managed_by,
                    approval_tier        = excluded.approval_tier,
                    updated_at           = excluded.updated_at
                """,
                (
                    scope.tenant_id,
                    scope.agent_id,
                    scope.scope_id,
                    json.dumps(sorted(scope.native_tools)),
                    json.dumps(scope.policy_overlay),
                    json.dumps(list(scope.views)),
                    int(scope.cerebro_unrestricted),
                    int(scope.enforced),
                    scope.updated_by,
                    scope.managed_by,
                    scope.approval_tier,
                    scope.updated_at.isoformat(),
                ),
            )

    def get_scope(self, agent_id: str, tenant_id: str) -> AgentAccessScope | None:
        """Return the scope for (agent_id, tenant_id), or None if none exists."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_access_scopes WHERE agent_id=? AND tenant_id=?",
                (agent_id, tenant_id),
            ).fetchone()
        return self._row_to_scope(row) if row is not None else None

    def list_by_agent(self, agent_id: str) -> list[AgentAccessScope]:
        """Return every scope row for *agent_id*, across tenants."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_access_scopes WHERE agent_id=? ORDER BY updated_at DESC",
                (agent_id,),
            ).fetchall()
        return [self._row_to_scope(r) for r in rows]

    @staticmethod
    def _row_to_scope(row: sqlite3.Row) -> AgentAccessScope:
        return AgentAccessScope(
            scope_id=row["scope_id"],
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            updated_by=row["updated_by"],
            native_tools=frozenset(json.loads(row["native_tools"])),
            policy_overlay=json.loads(row["policy_overlay"]),
            views=tuple(json.loads(row["views"])),
            cerebro_unrestricted=bool(row["cerebro_unrestricted"]),
            enforced=bool(row["enforced"]),
            managed_by=row["managed_by"],
            approval_tier=(row["approval_tier"] if "approval_tier" in row.keys() else "standard"),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
