"""SQLiteProviderRepository — persistencia de Providers en SQLite WAL.

Schema:

    providers
      provider_id       TEXT PK (uuid)
      alias             TEXT UNIQUE
      kind              TEXT
      base_url          TEXT
      default_model     TEXT
      enabled           INTEGER (0/1)
      is_active         INTEGER (0/1) — solo uno puede estar 1
      api_key_ciphertext BLOB (NULLABLE)
      created_at        TEXT (iso)

Las API keys cifradas viven aqui mismo. Las decifradas SOLO se exponen al
LiteLLM bridge en el momento de la llamada.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from hermes.shell_server.providers.domain import (
    Provider,
    ProviderAliasConflict,
    ProviderConnectivity,
    ProviderKind,
    ProviderNotFound,
)
from hermes.shell_server.security.secrets import SecretsVault


_SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
  provider_id        TEXT PRIMARY KEY,
  alias              TEXT NOT NULL UNIQUE,
  kind               TEXT NOT NULL,
  base_url           TEXT,
  default_model      TEXT NOT NULL,
  enabled            INTEGER NOT NULL DEFAULT 1,
  is_active          INTEGER NOT NULL DEFAULT 0,
  api_key_ciphertext BLOB,
  connectivity       TEXT NOT NULL DEFAULT 'unknown',
  last_checked_at    TEXT,
  created_at         TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS providers_one_active
  ON providers (is_active) WHERE is_active = 1;
"""


class SQLiteProviderRepository:
    """Persistencia + secrets vault."""

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

    # ----------------------------------------------------------------
    # Add / update / delete
    # ----------------------------------------------------------------
    def add(self, *, provider: Provider, api_key: str | None) -> Provider:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT provider_id FROM providers WHERE alias = ?",
                (provider.alias,),
            ).fetchone()
            if existing is not None:
                raise ProviderAliasConflict(provider.alias)

            api_key_blob: bytes | None = None
            if api_key:
                api_key_blob = self._vault.encrypt(
                    secret_id=str(provider.provider_id), plaintext=api_key
                )

            conn.execute(
                """
                INSERT INTO providers (
                  provider_id, alias, kind, base_url, default_model,
                  enabled, is_active, api_key_ciphertext, connectivity,
                  last_checked_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(provider.provider_id),
                    provider.alias,
                    provider.kind.value,
                    provider.base_url,
                    provider.default_model,
                    1 if provider.enabled else 0,
                    1 if provider.is_active else 0,
                    api_key_blob,
                    provider.connectivity.value,
                    provider.last_checked_at.isoformat()
                    if provider.last_checked_at
                    else None,
                    provider.created_at.isoformat(),
                ),
            )
        return provider

    def update(self, *, provider: Provider, api_key: str | None = None) -> Provider:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT provider_id FROM providers WHERE provider_id = ?",
                (str(provider.provider_id),),
            ).fetchone()
            if row is None:
                raise ProviderNotFound(str(provider.provider_id))

            api_key_blob: bytes | None = None
            if api_key:
                api_key_blob = self._vault.encrypt(
                    secret_id=str(provider.provider_id), plaintext=api_key
                )
                conn.execute(
                    "UPDATE providers SET api_key_ciphertext = ? "
                    "WHERE provider_id = ?",
                    (api_key_blob, str(provider.provider_id)),
                )

            conn.execute(
                """
                UPDATE providers SET
                    alias = ?, kind = ?, base_url = ?, default_model = ?,
                    enabled = ?, connectivity = ?, last_checked_at = ?
                WHERE provider_id = ?
                """,
                (
                    provider.alias,
                    provider.kind.value,
                    provider.base_url,
                    provider.default_model,
                    1 if provider.enabled else 0,
                    provider.connectivity.value,
                    provider.last_checked_at.isoformat()
                    if provider.last_checked_at
                    else None,
                    str(provider.provider_id),
                ),
            )
        return provider

    def delete(self, *, provider_id: UUID) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM providers WHERE provider_id = ?",
                (str(provider_id),),
            )
            if cursor.rowcount == 0:
                raise ProviderNotFound(str(provider_id))

    def set_active(self, *, provider_id: UUID) -> None:
        """Marca uno como activo (deactiva el resto)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT provider_id FROM providers WHERE provider_id = ?",
                (str(provider_id),),
            ).fetchone()
            if row is None:
                raise ProviderNotFound(str(provider_id))
            conn.execute("UPDATE providers SET is_active = 0")
            conn.execute(
                "UPDATE providers SET is_active = 1 WHERE provider_id = ?",
                (str(provider_id),),
            )

    # ----------------------------------------------------------------
    # Queries
    # ----------------------------------------------------------------
    def list_all(self) -> list[Provider]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM providers ORDER BY created_at"
            ).fetchall()
        return [self._row_to_provider(r) for r in rows]

    def get(self, *, provider_id: UUID) -> Provider:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM providers WHERE provider_id = ?",
                (str(provider_id),),
            ).fetchone()
        if row is None:
            raise ProviderNotFound(str(provider_id))
        return self._row_to_provider(row)

    def get_active(self) -> Provider | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM providers WHERE is_active = 1"
            ).fetchone()
        if row is None:
            return None
        return self._row_to_provider(row)

    def reveal_api_key(self, *, provider_id: UUID) -> str | None:
        """Devuelve plaintext de la API key. SOLO para el LiteLLM bridge."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT api_key_ciphertext FROM providers WHERE provider_id = ?",
                (str(provider_id),),
            ).fetchone()
        if row is None:
            raise ProviderNotFound(str(provider_id))
        blob = row["api_key_ciphertext"]
        if blob is None:
            return None
        return self._vault.decrypt(
            secret_id=str(provider_id), blob=bytes(blob)
        )

    # ----------------------------------------------------------------
    # Hydration
    # ----------------------------------------------------------------
    def _row_to_provider(self, row) -> Provider:
        return Provider(
            provider_id=UUID(row["provider_id"]),
            alias=row["alias"],
            kind=ProviderKind(row["kind"]),
            base_url=row["base_url"],
            has_api_key=row["api_key_ciphertext"] is not None,
            default_model=row["default_model"],
            enabled=bool(row["enabled"]),
            is_active=bool(row["is_active"]),
            connectivity=ProviderConnectivity(row["connectivity"]),
            last_checked_at=(
                datetime.fromisoformat(row["last_checked_at"])
                if row["last_checked_at"]
                else None
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
