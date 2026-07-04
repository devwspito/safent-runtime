"""security_hook._tenant_remote_approval_enabled() — REAL accessor (Fase 2
Phase 4b). Supersedes Phase 4a's stub (ALWAYS False).

True ONLY when: (a) the instance is paired/associated AND (b) the tenant's
applied license carries remote_approval_enabled=True. Fail-safe False on ANY
error — unpaired, missing store, corrupt license, DB unavailable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes.runtime import security_hook

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_flag_cache():
    """The accessor is TTL-cached (10s) — reset between tests so one test's
    result never leaks into the next."""
    security_hook._remote_approval_flag_cache["expires_at"] = 0.0
    yield
    security_hook._remote_approval_flag_cache["expires_at"] = 0.0


def _seed_association(db_path: Path, *, license_data: dict) -> None:
    """Write a minimal instance_association row directly — avoids depending
    on SecretsVault (the vault is only touched by reveal_instance_secret,
    never by is_associated()/get())."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE instance_association (
          id INTEGER PRIMARY KEY CHECK(id = 1),
          instance_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          paired_at TEXT NOT NULL,
          cloud_endpoint TEXT NOT NULL,
          signing_pubkey_hex TEXT NOT NULL DEFAULT '',
          license_json TEXT NOT NULL DEFAULT '{}',
          last_applied_version INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'active',
          instance_secret_ciphertext BLOB
        )
        """
    )
    conn.execute(
        "INSERT INTO instance_association "
        "(id, instance_id, tenant_id, paired_at, cloud_endpoint, license_json, state) "
        "VALUES (1, 'inst-1', 'tenant-1', ?, 'https://cloud.example.com', ?, 'active')",
        (datetime.now(tz=UTC).isoformat(), json.dumps(license_data)),
    )
    conn.commit()
    conn.close()


def _with_db(monkeypatch, tmp_path: Path, *, license_data: dict | None) -> Path:
    db_path = tmp_path / "shell-state.db"
    if license_data is not None:
        _seed_association(db_path, license_data=license_data)
    monkeypatch.setenv("HERMES_SHELL_DB", str(db_path))
    return db_path


class TestTenantRemoteApprovalFlag:
    def test_unpaired_instance_returns_false(self, monkeypatch, tmp_path) -> None:
        _with_db(monkeypatch, tmp_path, license_data=None)  # no DB at all

        with patch("hermes.shell_server.security.secrets.SecretsVault", return_value=MagicMock()):
            assert security_hook._read_tenant_remote_approval_flag() is False

    def test_paired_without_flag_returns_false(self, monkeypatch, tmp_path) -> None:
        _with_db(monkeypatch, tmp_path, license_data={"plan": "starter", "views": ["chat"]})

        with patch("hermes.shell_server.security.secrets.SecretsVault", return_value=MagicMock()):
            assert security_hook._read_tenant_remote_approval_flag() is False

    def test_paired_with_flag_true_returns_true(self, monkeypatch, tmp_path) -> None:
        _with_db(
            monkeypatch, tmp_path,
            license_data={"plan": "enterprise", "remote_approval_enabled": True},
        )

        with patch("hermes.shell_server.security.secrets.SecretsVault", return_value=MagicMock()):
            assert security_hook._read_tenant_remote_approval_flag() is True

    def test_store_construction_error_fails_safe_false(self, monkeypatch, tmp_path) -> None:
        _with_db(
            monkeypatch, tmp_path,
            license_data={"remote_approval_enabled": True},
        )

        with patch(
            "hermes.instance.association_store.SQLiteAssociationStore",
            side_effect=RuntimeError("db locked"),
        ):
            assert security_hook._read_tenant_remote_approval_flag() is False

    def test_public_accessor_caches_for_the_ttl_window(self, monkeypatch, tmp_path) -> None:
        """_tenant_remote_approval_enabled() is TTL-cached — a DB flip within
        the TTL window is not observed until the cache expires."""
        db_path = _with_db(monkeypatch, tmp_path, license_data={"remote_approval_enabled": True})

        with patch("hermes.shell_server.security.secrets.SecretsVault", return_value=MagicMock()):
            assert security_hook._tenant_remote_approval_enabled() is True

            # Flip the flag directly in the DB — the cached True must still win
            # until the TTL expires (monotonic clock hasn't moved forward).
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "UPDATE instance_association SET license_json = ?",
                (json.dumps({"remote_approval_enabled": False}),),
            )
            conn.commit()
            conn.close()

            assert security_hook._tenant_remote_approval_enabled() is True  # still cached
