"""Integration repository: set/get/reveal cycle with encryption.

Mirrors the style of test_provider_config_source.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.shell_server.integrations.domain import IntegrationNotFound
from hermes.shell_server.integrations.repo import SQLiteIntegrationsRepository
from hermes.shell_server.security import secrets as secrets_mod
from hermes.shell_server.security.secrets import SecretsVault

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def master_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch master.key to a deterministic test key in tmp_path."""
    key_file = tmp_path / "master.key"
    key_file.write_bytes(b"\xab" * 32)
    monkeypatch.setattr(secrets_mod, "_MASTER_KEY_PATH", key_file)
    return key_file


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "shell-state.db"


def _repo(db_path: Path) -> SQLiteIntegrationsRepository:
    return SQLiteIntegrationsRepository(db_path=db_path, vault=SecretsVault())


# ----------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------


def test_set_credential_returns_integration_with_has_key(db_path: Path) -> None:
    repo = _repo(db_path)
    integration = repo.set_credential(
        kind="composio", api_key="csk-test-abc", entity_id="default"
    )
    assert integration.kind == "composio"
    assert integration.has_api_key is True
    assert integration.enabled is True
    assert integration.entity_id == "default"


def test_reveal_api_key_returns_correct_plaintext(db_path: Path) -> None:
    repo = _repo(db_path)
    repo.set_credential(kind="composio", api_key="secret-key-xyz")
    revealed = repo.reveal_api_key(kind="composio")
    assert revealed == "secret-key-xyz"


def test_reveal_api_key_is_not_stored_in_plaintext(db_path: Path) -> None:
    """The raw SQLite file must not contain the plaintext key."""
    repo = _repo(db_path)
    repo.set_credential(kind="composio", api_key="super-secret-key-789")
    raw_bytes = db_path.read_bytes()
    assert b"super-secret-key-789" not in raw_bytes


def test_get_returns_integration(db_path: Path) -> None:
    repo = _repo(db_path)
    repo.set_credential(kind="composio", api_key="k", entity_id="my-entity")
    integration = repo.get(kind="composio")
    assert integration.entity_id == "my-entity"


def test_get_or_none_returns_none_when_absent(db_path: Path) -> None:
    repo = _repo(db_path)
    assert repo.get_or_none(kind="composio") is None


# ----------------------------------------------------------------
# Upsert behaviour
# ----------------------------------------------------------------


def test_set_credential_overwrites_existing_key(db_path: Path) -> None:
    repo = _repo(db_path)
    repo.set_credential(kind="composio", api_key="first-key")
    repo.set_credential(kind="composio", api_key="second-key")
    assert repo.reveal_api_key(kind="composio") == "second-key"


def test_set_credential_updates_entity_id(db_path: Path) -> None:
    repo = _repo(db_path)
    repo.set_credential(kind="composio", api_key="k", entity_id="ent-a")
    repo.set_credential(kind="composio", api_key="k", entity_id="ent-b")
    assert repo.get(kind="composio").entity_id == "ent-b"


# ----------------------------------------------------------------
# Error cases
# ----------------------------------------------------------------


def test_get_raises_integration_not_found(db_path: Path) -> None:
    repo = _repo(db_path)
    with pytest.raises(IntegrationNotFound):
        repo.get(kind="composio")


def test_reveal_raises_integration_not_found(db_path: Path) -> None:
    repo = _repo(db_path)
    with pytest.raises(IntegrationNotFound):
        repo.reveal_api_key(kind="composio")


# ----------------------------------------------------------------
# Cross-vault isolation: wrong key cannot decrypt
# ----------------------------------------------------------------


def test_different_master_key_cannot_decrypt(db_path: Path) -> None:
    """Encrypting with key-A and decrypting with key-B must raise."""
    from cryptography.exceptions import InvalidTag  # noqa: PLC0415

    vault_a = SecretsVault(master_key=b"\xaa" * 32)
    vault_b = SecretsVault(master_key=b"\xbb" * 32)

    repo_a = SQLiteIntegrationsRepository(db_path=db_path, vault=vault_a)
    repo_a.set_credential(kind="composio", api_key="secret-for-a")

    repo_b = SQLiteIntegrationsRepository(db_path=db_path, vault=vault_b)
    with pytest.raises((InvalidTag, Exception)):
        repo_b.reveal_api_key(kind="composio")
