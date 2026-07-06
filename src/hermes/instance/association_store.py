"""SQLiteAssociationStore — persists the enterprise pairing row.

One and only one row is allowed (id=1).  The instance_secret is stored
encrypted via SecretsVault (AES-GCM-256); it is never returned in the
public InstanceAssociation dataclass.

Schema is idempotent (CREATE TABLE IF NOT EXISTS + PRAGMA table_info
migrations), so adding columns later is safe.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes.shell_server.security.secrets import SecretsVault

logger = logging.getLogger("hermes.instance.association_store")

_SECRET_ID = "instance:secret"  # AAD label for AES-GCM (stable — do NOT change after rows are written)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instance_association (
  id                        INTEGER PRIMARY KEY CHECK(id = 1),
  instance_id               TEXT    NOT NULL,
  tenant_id                 TEXT    NOT NULL,
  paired_at                 TEXT    NOT NULL,
  cloud_endpoint            TEXT    NOT NULL,
  signing_pubkey_hex        TEXT    NOT NULL DEFAULT '',
  license_json              TEXT    NOT NULL DEFAULT '{}',
  last_applied_version      INTEGER NOT NULL DEFAULT 0,
  state                     TEXT    NOT NULL DEFAULT 'active',
  instance_secret_ciphertext BLOB,
  directory_json            TEXT    NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True, slots=True)
class InstanceAssociation:
    """Public view of the pairing — no secret material."""

    instance_id: str
    tenant_id: str
    paired_at: str          # ISO-8601 UTC
    cloud_endpoint: str
    signing_pubkey_hex: str
    license: dict           # noqa: ANN001 — arbitrary JSON from the control plane
    last_applied_version: int
    state: str              # "active" | "revoked"
    # Fase 3 (department-scoped visibility): the DirectorySpec dump
    # ({"entries": [...]}) delivered by the latest applied bundle, or None
    # when no directory was pushed (visibility_scope="all", the default —
    # the associate falls back to today's local-roster-only behaviour).
    directory: dict | None = None


class SQLiteAssociationStore:
    """Single-row SQLite store for the enterprise pairing.

    The vault is used only for the instance_secret (write on pair,
    read on reveal_instance_secret).  All other columns are stored
    as plaintext — they are non-sensitive configuration.
    """

    def __init__(self, *, db_path: Path, vault: "SecretsVault") -> None:
        self._db_path = db_path
        self._vault = vault
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("PRAGMA journal_mode=WAL;")
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @property
    def db_path(self) -> Path:
        """Expose the DB path for co-located stores (e.g. instance_identity)."""
        return self._db_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_associated(self) -> bool:
        """True when a pairing row with state='active' exists."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM instance_association WHERE id = 1"
            ).fetchone()
        return row is not None and row["state"] == "active"

    def get(self) -> InstanceAssociation | None:
        """Return the association (no secret) or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM instance_association WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return self._row_to_association(row)

    def reveal_instance_secret(self) -> str | None:
        """Decrypt and return the instance secret.

        Returns None when no pairing exists or the ciphertext column is NULL.
        The plaintext is NEVER logged.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT instance_secret_ciphertext FROM instance_association WHERE id = 1"
            ).fetchone()
        if row is None or row["instance_secret_ciphertext"] is None:
            return None
        blob = bytes(row["instance_secret_ciphertext"])
        return self._vault.decrypt(secret_id=_SECRET_ID, blob=blob)

    def save(self, *, association: InstanceAssociation, instance_secret: str) -> None:
        """Upsert the single pairing row, encrypting the secret."""
        blob = self._vault.encrypt(secret_id=_SECRET_ID, plaintext=instance_secret)
        license_json = json.dumps(association.license)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO instance_association (
                  id, instance_id, tenant_id, paired_at, cloud_endpoint,
                  signing_pubkey_hex, license_json, last_applied_version,
                  state, instance_secret_ciphertext
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  instance_id                = excluded.instance_id,
                  tenant_id                  = excluded.tenant_id,
                  paired_at                  = excluded.paired_at,
                  cloud_endpoint             = excluded.cloud_endpoint,
                  signing_pubkey_hex         = excluded.signing_pubkey_hex,
                  license_json               = excluded.license_json,
                  last_applied_version       = excluded.last_applied_version,
                  state                      = excluded.state,
                  instance_secret_ciphertext = excluded.instance_secret_ciphertext
                """,
                (
                    association.instance_id,
                    association.tenant_id,
                    association.paired_at,
                    association.cloud_endpoint,
                    association.signing_pubkey_hex,
                    license_json,
                    association.last_applied_version,
                    association.state,
                    blob,
                ),
            )
        logger.info("hermes.instance.association_saved", extra={"tenant_id": association.tenant_id})

    def set_last_applied_version(self, version: int) -> None:
        """Advance the last-applied policy version (monotonic; only call on success)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE instance_association SET last_applied_version = ? WHERE id = 1",
                (int(version),),
            )
        logger.info(
            "hermes.instance.last_applied_version_updated",
            extra={"version": version},
        )

    def update_license(self, license_data: dict) -> None:
        """Persist the license section of the latest applied bundle."""
        import json as _json  # noqa: PLC0415 — avoid top-level import cycle risk

        license_json = _json.dumps(license_data)
        with self._connect() as conn:
            conn.execute(
                "UPDATE instance_association SET license_json = ? WHERE id = 1",
                (license_json,),
            )
        logger.info("hermes.instance.license_updated")

    def update_directory(self, directory: dict | None) -> None:
        """Persist the Fase-3 department-scoped directory (replace-on-apply).

        `directory` is the DirectorySpec dump ({"entries": [...]}). None
        clears it — the roster/delegation UX then fall back to today's
        local-roster-only behaviour (mirrors update_license's overwrite
        semantics; presentation data only, the cloud enforces the
        department gate authoritatively).
        """
        directory_json = json.dumps(directory) if directory is not None else ""
        with self._connect() as conn:
            conn.execute(
                "UPDATE instance_association SET directory_json = ? WHERE id = 1",
                (directory_json,),
            )
        logger.info(
            "hermes.instance.directory_updated",
            extra={"entries": len(directory.get("entries", [])) if directory else 0},
        )

    def mark_revoked(self) -> None:
        """Flip state to 'revoked' without deleting the row (audit trail)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE instance_association SET state = 'revoked' WHERE id = 1"
            )
        logger.info("hermes.instance.association_revoked")

    def clear(self) -> None:
        """Securely delete the pairing row (unpair / factory reset).

        Sequence:
          1. Overwrite instance_secret_ciphertext with NULL to discard the
             ciphertext in-place before the row is deleted.
          2. DELETE the row.
          3. PRAGMA wal_checkpoint(TRUNCATE) — flush and truncate the WAL so
             the ciphertext does not linger in the write-ahead log.
          4. VACUUM — reclaim pages and prevent ciphertext recovery from
             free-list pages.

        Use clear() for operator-initiated unpair (no audit trail needed).
        Use mark_revoked() when you need to preserve the row for auditing.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE instance_association SET instance_secret_ciphertext = NULL WHERE id = 1"
            )
            conn.execute("DELETE FROM instance_association WHERE id = 1")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # VACUUM must run outside the WAL transaction (it implicitly commits).
        with self._connect() as conn:
            conn.execute("VACUUM")
        logger.info("hermes.instance.association_cleared")

    def edition(self) -> str:
        """Return 'associate' when paired and active, 'community' otherwise."""
        return "associate" if self.is_associated() else "community"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Idempotent column additions for future schema evolution."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(instance_association)")}
        if "signing_pubkey_hex" not in cols:
            conn.execute(
                "ALTER TABLE instance_association ADD COLUMN signing_pubkey_hex TEXT NOT NULL DEFAULT ''"
            )
        if "directory_json" not in cols:
            conn.execute(
                "ALTER TABLE instance_association ADD COLUMN directory_json TEXT NOT NULL DEFAULT ''"
            )

    def _row_to_association(self, row: sqlite3.Row) -> InstanceAssociation:
        try:
            license_data: dict = json.loads(row["license_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            license_data = {}
        directory_raw = row["directory_json"] or ""
        try:
            directory_data: dict | None = json.loads(directory_raw) if directory_raw else None
        except (json.JSONDecodeError, TypeError):
            directory_data = None
        return InstanceAssociation(
            instance_id=row["instance_id"],
            tenant_id=row["tenant_id"],
            paired_at=row["paired_at"],
            cloud_endpoint=row["cloud_endpoint"],
            signing_pubkey_hex=row["signing_pubkey_hex"] or "",
            license=license_data,
            last_applied_version=int(row["last_applied_version"] or 0),
            state=row["state"],
            directory=directory_data,
        )
