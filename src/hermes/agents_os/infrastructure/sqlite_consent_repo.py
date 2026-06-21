"""SQLiteConsentRepository — persistence for ConsentManager (FR-054).

Persists grant/revoke decisions so that consent state survives runtime
restarts (hermes-runtime.service uses Restart=always; in-memory loss
would silently wipe all active consents).

Schema is created on first connect (migration-free, single-node).
Thread-safe: each call opens + closes its own connection.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from hermes.agents_os.application.consent_manager import (
    Capability,
    Consent,
    ConsentScope,
)

_DDL = """
CREATE TABLE IF NOT EXISTS consent_grants (
    consent_id      TEXT PRIMARY KEY,
    tenant_id       TEXT,
    human_operator_id TEXT NOT NULL,
    capability      TEXT NOT NULL,
    scope           TEXT NOT NULL,
    granted_at      TEXT NOT NULL,
    expires_at      TEXT,
    revoked_at      TEXT,
    usage_count     INTEGER NOT NULL DEFAULT 0,
    last_used_at    TEXT
);
CREATE INDEX IF NOT EXISTS consent_grants_op_cap
    ON consent_grants (human_operator_id, capability);
"""


class SQLiteConsentRepository:
    """SQLite-backed persistence for Consent records."""

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_DDL)

    def save(self, consent: Consent) -> None:
        """Upsert a consent record (insert or replace on consent_id)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO consent_grants
                    (consent_id, tenant_id, human_operator_id, capability, scope,
                     granted_at, expires_at, revoked_at, usage_count, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(consent.consent_id),
                    str(consent.tenant_id) if consent.tenant_id else None,
                    str(consent.human_operator_id),
                    consent.capability.value,
                    consent.scope.value,
                    consent.granted_at.isoformat(),
                    consent.expires_at.isoformat() if consent.expires_at else None,
                    consent.revoked_at.isoformat() if consent.revoked_at else None,
                    consent.usage_count,
                    consent.last_used_at.isoformat() if consent.last_used_at else None,
                ),
            )

    def load_active(self) -> list[Consent]:
        """Load all non-revoked, non-expired consents."""
        now_iso = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM consent_grants
                WHERE revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (now_iso,),
            ).fetchall()
        return [_row_to_consent(r) for r in rows]

    def load_all(self) -> list[Consent]:
        """Load all consent records (including revoked/expired)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM consent_grants").fetchall()
        return [_row_to_consent(r) for r in rows]


def _row_to_consent(row: sqlite3.Row) -> Consent:
    def _dt(val: str | None) -> datetime | None:
        if val is None:
            return None
        return datetime.fromisoformat(val)

    return Consent(
        consent_id=UUID(row["consent_id"]),
        tenant_id=UUID(row["tenant_id"]) if row["tenant_id"] else None,
        human_operator_id=UUID(row["human_operator_id"]),
        capability=Capability(row["capability"]),
        scope=ConsentScope(row["scope"]),
        granted_at=datetime.fromisoformat(row["granted_at"]),
        expires_at=_dt(row["expires_at"]),
        revoked_at=_dt(row["revoked_at"]),
        usage_count=row["usage_count"],
        last_used_at=_dt(row["last_used_at"]),
    )
