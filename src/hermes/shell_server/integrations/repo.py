"""SQLiteIntegrationsRepository — persistence for Integration rows.

Schema lives in shell-state.db alongside providers, training_sessions etc.
API keys are stored encrypted (AES-GCM-256 via SecretsVault), never in
plaintext.  Secrets NEVER appear in logs or API responses.

One row per kind (UNIQUE constraint).  set_credential upserts so callers
never need to check for existence first.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from hermes.shell_server.integrations.domain import (
    Integration,
    IntegrationNotFound,
)
from hermes.shell_server.security.secrets import SecretsVault

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS integrations (
  kind               TEXT PRIMARY KEY,
  enabled            INTEGER NOT NULL DEFAULT 1,
  entity_id          TEXT NOT NULL DEFAULT 'default',
  api_key_ciphertext BLOB,
  created_at         TEXT NOT NULL
);
"""

# Secret-id prefix used as AAD for AES-GCM.  Stable; do NOT change after
# rows are written — it is part of the authenticated data that protects
# against key reuse across different integration kinds.
_SECRET_ID_PREFIX: str = "integration:"  # noqa: S105 (not a password — an AAD prefix)


class SQLiteIntegrationsRepository:
    """Persist and retrieve Integration records with encrypted credentials."""

    def __init__(self, *, db_path: Path, vault: SecretsVault) -> None:
        self._db_path = db_path
        self._vault = vault
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("PRAGMA journal_mode=WAL;")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _secret_id(self, kind: str) -> str:
        return f"{_SECRET_ID_PREFIX}{kind}"

    # ----------------------------------------------------------------
    # Credential write (upsert)
    # ----------------------------------------------------------------

    def set_credential(
        self,
        *,
        kind: str,
        api_key: str,
        entity_id: str = "default",
        enabled: bool = True,
    ) -> Integration:
        """Store (or overwrite) the encrypted API key for `kind`.

        Creates the row if it does not exist yet (upsert).
        """
        blob = self._vault.encrypt(
            secret_id=self._secret_id(kind), plaintext=api_key
        )
        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO integrations (kind, enabled, entity_id, api_key_ciphertext, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(kind) DO UPDATE SET
                    enabled            = excluded.enabled,
                    entity_id          = excluded.entity_id,
                    api_key_ciphertext = excluded.api_key_ciphertext
                """,
                (kind, 1 if enabled else 0, entity_id, blob, now),
            )
        logger.info("hermes.integrations.credential_stored", extra={"kind": kind})
        return Integration(
            kind=kind,
            has_api_key=True,
            enabled=enabled,
            entity_id=entity_id,
            created_at=datetime.fromisoformat(now),
        )

    # ----------------------------------------------------------------
    # Queries
    # ----------------------------------------------------------------

    def get(self, *, kind: str) -> Integration:
        """Return the Integration for `kind`, raising IntegrationNotFound if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM integrations WHERE kind = ?", (kind,)
            ).fetchone()
        if row is None:
            raise IntegrationNotFound(kind)
        return self._row_to_integration(row)

    def get_or_none(self, *, kind: str) -> Integration | None:
        """Return the Integration or None."""
        try:
            return self.get(kind=kind)
        except IntegrationNotFound:
            return None

    # ----------------------------------------------------------------
    # Secret reveal — ONLY for outbound HTTP calls to Composio
    # ----------------------------------------------------------------

    def reveal_api_key(self, *, kind: str) -> str | None:
        """Decrypt and return the API key for `kind`.

        Returns None if no key is stored.  The plaintext is NEVER logged.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT api_key_ciphertext FROM integrations WHERE kind = ?",
                (kind,),
            ).fetchone()
        if row is None:
            raise IntegrationNotFound(kind)
        blob = row["api_key_ciphertext"]
        if blob is None:
            return None
        return self._vault.decrypt(
            secret_id=self._secret_id(kind), blob=bytes(blob)
        )

    # ----------------------------------------------------------------
    # Hydration
    # ----------------------------------------------------------------

    def _row_to_integration(self, row: sqlite3.Row) -> Integration:
        return Integration(
            kind=row["kind"],
            has_api_key=row["api_key_ciphertext"] is not None,
            enabled=bool(row["enabled"]),
            entity_id=row["entity_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
