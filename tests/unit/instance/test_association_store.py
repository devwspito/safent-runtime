"""SQLiteAssociationStore — unit tests.

Tests cover:
  - save → get roundtrip (all public fields preserved)
  - save → reveal_instance_secret roundtrip (AES-GCM encrypted)
  - Secret NOT stored in plaintext (raw DB bytes check)
  - edition() returns 'community' before pair, 'associate' after
  - clear() → get() returns None, edition() returns 'community'
  - mark_revoked() → state='revoked', is_associated()=False
  - Single-row enforcement (second save upserts, does not add a second row)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore
from hermes.shell_server.security import secrets as secrets_mod
from hermes.shell_server.security.secrets import SecretsVault

pytestmark = pytest.mark.unit

_TENANT_ID = "00000000-0000-0000-0000-000000000001"
_INSTANCE_ID = "aaaaaaaa-0000-5000-8000-000000000001"
_SECRET = "sk-test-instance-secret-12345"


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_master_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a deterministic test master key without touching the filesystem."""
    key_file = tmp_path / "master.key"
    key_file.write_bytes(b"\xca" * 32)
    monkeypatch.setattr(secrets_mod, "_MASTER_KEY_PATH", key_file)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "shell-state.db"


@pytest.fixture
def vault() -> SecretsVault:
    return SecretsVault(master_key=b"\xca" * 32)


def _store(db_path: Path, vault: SecretsVault) -> SQLiteAssociationStore:
    return SQLiteAssociationStore(db_path=db_path, vault=vault)


def _make_association(**overrides: object) -> InstanceAssociation:
    defaults: dict[str, object] = {
        "instance_id": _INSTANCE_ID,
        "tenant_id": _TENANT_ID,
        "paired_at": "2026-06-26T10:00:00+00:00",
        "cloud_endpoint": "https://cloud.safent.run",
        "signing_pubkey_hex": "deadbeef",
        "license": {"plan": "starter", "seats": 5},
        "last_applied_version": 0,
        "state": "active",
    }
    defaults.update(overrides)
    return InstanceAssociation(**defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------------
# is_associated / edition — empty store
# ------------------------------------------------------------------


class TestEmptyStore:
    def test_is_associated_false_when_no_row(self, db_path: Path, vault: SecretsVault) -> None:
        store = _store(db_path, vault)
        assert store.is_associated() is False

    def test_get_returns_none_when_no_row(self, db_path: Path, vault: SecretsVault) -> None:
        store = _store(db_path, vault)
        assert store.get() is None

    def test_edition_community_when_no_row(self, db_path: Path, vault: SecretsVault) -> None:
        store = _store(db_path, vault)
        assert store.edition() == "community"

    def test_reveal_secret_returns_none_when_no_row(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        assert store.reveal_instance_secret() is None


# ------------------------------------------------------------------
# save → get roundtrip
# ------------------------------------------------------------------


class TestSaveGet:
    def test_get_returns_all_fields(self, db_path: Path, vault: SecretsVault) -> None:
        store = _store(db_path, vault)
        assoc = _make_association()
        store.save(association=assoc, instance_secret=_SECRET)
        result = store.get()
        assert result is not None
        assert result.instance_id == _INSTANCE_ID
        assert result.tenant_id == _TENANT_ID
        assert result.cloud_endpoint == "https://cloud.safent.run"
        assert result.signing_pubkey_hex == "deadbeef"
        assert result.license == {"plan": "starter", "seats": 5}
        assert result.last_applied_version == 0
        assert result.state == "active"

    def test_is_associated_true_after_save(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        assert store.is_associated() is True

    def test_edition_associate_after_save(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        assert store.edition() == "associate"


# ------------------------------------------------------------------
# Secret encryption roundtrip
# ------------------------------------------------------------------


class TestSecretEncryption:
    def test_reveal_returns_correct_plaintext(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        revealed = store.reveal_instance_secret()
        assert revealed == _SECRET

    def test_secret_not_stored_in_plaintext(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        raw = db_path.read_bytes()
        assert _SECRET.encode() not in raw

    def test_wrong_master_key_cannot_decrypt(self, db_path: Path) -> None:
        """Encrypting with vault-A and decrypting with vault-B must raise."""
        from cryptography.exceptions import InvalidTag  # noqa: PLC0415

        vault_a = SecretsVault(master_key=b"\xaa" * 32)
        vault_b = SecretsVault(master_key=b"\xbb" * 32)
        store_a = SQLiteAssociationStore(db_path=db_path, vault=vault_a)
        store_a.save(association=_make_association(), instance_secret=_SECRET)
        store_b = SQLiteAssociationStore(db_path=db_path, vault=vault_b)
        with pytest.raises((InvalidTag, Exception)):
            store_b.reveal_instance_secret()


# ------------------------------------------------------------------
# Single-row enforcement (upsert)
# ------------------------------------------------------------------


class TestSingleRow:
    def test_second_save_upserts_not_inserts(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        """Two save() calls must result in exactly one row in the DB."""
        import sqlite3  # noqa: PLC0415

        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        store.save(
            association=_make_association(tenant_id="00000000-0000-0000-0000-000000000002"),
            instance_secret="updated-secret",
        )
        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM instance_association"
            ).fetchone()[0]
        assert count == 1

    def test_second_save_overwrites_tenant_id(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        new_tenant = "00000000-0000-0000-0000-000000000099"
        store.save(
            association=_make_association(tenant_id=new_tenant),
            instance_secret="new-secret",
        )
        result = store.get()
        assert result is not None
        assert result.tenant_id == new_tenant

    def test_second_save_updates_secret(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret="first-secret")
        store.save(
            association=_make_association(tenant_id="00000000-0000-0000-0000-000000000002"),
            instance_secret="second-secret",
        )
        assert store.reveal_instance_secret() == "second-secret"


# ------------------------------------------------------------------
# mark_revoked
# ------------------------------------------------------------------


class TestMarkRevoked:
    def test_is_associated_false_after_revoke(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        store.mark_revoked()
        assert store.is_associated() is False

    def test_get_still_returns_row_after_revoke(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        store.mark_revoked()
        result = store.get()
        assert result is not None
        assert result.state == "revoked"

    def test_edition_community_after_revoke(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        store.mark_revoked()
        assert store.edition() == "community"


# ------------------------------------------------------------------
# clear (unpair)
# ------------------------------------------------------------------


class TestClear:
    def test_get_none_after_clear(self, db_path: Path, vault: SecretsVault) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        store.clear()
        assert store.get() is None

    def test_is_associated_false_after_clear(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        store.clear()
        assert store.is_associated() is False

    def test_edition_community_after_clear(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        store.clear()
        assert store.edition() == "community"

    def test_reveal_secret_none_after_clear(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        store.clear()
        assert store.reveal_instance_secret() is None

    def test_no_ciphertext_remains_in_db_after_clear(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        """After clear(), the raw DB file must not contain the ciphertext blob.

        This is the P2 secure-delete requirement: the ciphertext is NULLed
        before the DELETE, then WAL is checkpointed, then VACUUM runs so no
        ciphertext lingers in WAL pages or free-list pages.
        """
        store = _store(db_path, vault)
        # Encrypt a known secret and capture its ciphertext bytes.
        store.save(association=_make_association(), instance_secret=_SECRET)
        # Read the ciphertext before clearing.
        import sqlite3  # noqa: PLC0415
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT instance_secret_ciphertext FROM instance_association WHERE id=1"
            ).fetchone()
        assert row is not None
        ciphertext = bytes(row[0])
        assert len(ciphertext) > 0

        store.clear()

        # After clear, the ciphertext bytes must not appear in the DB file.
        raw = db_path.read_bytes()
        assert ciphertext not in raw


# ------------------------------------------------------------------
# Directory (Fase 3 — department-scoped visibility)
# ------------------------------------------------------------------


class TestDirectory:
    def test_get_returns_none_directory_when_never_pushed(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        """A fresh pairing has no directory — get().directory is None."""
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        result = store.get()
        assert result is not None
        assert result.directory is None

    def test_update_directory_persists_entries(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        directory = {
            "entries": [
                {
                    "employee_id": "emp-1",
                    "agent_id": "agent-1",
                    "name": "Ada",
                    "department": "ventas",
                }
            ]
        }
        store.update_directory(directory)
        result = store.get()
        assert result is not None
        assert result.directory == directory

    def test_update_directory_none_clears_it(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        """A subsequent bundle with directory=None clears the previously stored one."""
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        store.update_directory({"entries": [{"employee_id": "e", "agent_id": "a", "name": "A", "department": "d"}]})
        assert store.get().directory is not None  # noqa: S101 — sanity precondition

        store.update_directory(None)

        result = store.get()
        assert result is not None
        assert result.directory is None

    def test_update_directory_empty_entries_is_present_not_none(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        """visibility_scope='none' -> {"entries": []} is a PRESENT directory
        (distinct from no directory at all)."""
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        store.update_directory({"entries": []})
        result = store.get()
        assert result is not None
        assert result.directory == {"entries": []}

    def test_directory_survives_unrelated_license_update(
        self, db_path: Path, vault: SecretsVault
    ) -> None:
        """update_license must not clobber a previously-stored directory."""
        store = _store(db_path, vault)
        store.save(association=_make_association(), instance_secret=_SECRET)
        directory = {"entries": [{"employee_id": "e", "agent_id": "a", "name": "A", "department": "d"}]}
        store.update_directory(directory)

        store.update_license({"plan": "pro", "max_agents": 10, "expires_at": "", "views": []})

        result = store.get()
        assert result is not None
        assert result.directory == directory
        assert result.license["plan"] == "pro"
