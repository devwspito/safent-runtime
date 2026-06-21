"""composio_config_source: credential loading from shell-state.db.

Mirrors test_provider_config_source.py style.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.runtime.composio_config_source import load_composio_credential
from hermes.shell_server.integrations.repo import SQLiteIntegrationsRepository
from hermes.shell_server.security import secrets as secrets_mod
from hermes.shell_server.security.secrets import SecretsVault

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def master_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    key_file = tmp_path / "master.key"
    key_file.write_bytes(b"\xcc" * 32)
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


def test_returns_credential_when_key_is_configured(db_path: Path) -> None:
    _repo(db_path).set_credential(
        kind="composio", api_key="csk-real-key", entity_id="ent-x"
    )
    cred = load_composio_credential(db_path)
    assert cred is not None
    assert cred.api_key == "csk-real-key"
    assert cred.entity_id == "ent-x"


# ----------------------------------------------------------------
# Fail-soft cases
# ----------------------------------------------------------------


def test_returns_none_when_db_absent(tmp_path: Path) -> None:
    assert load_composio_credential(tmp_path / "nope.db") is None


def test_returns_none_when_no_key_stored(db_path: Path) -> None:
    # DB exists, table exists, but no row for composio
    _repo(db_path)  # triggers schema creation
    assert load_composio_credential(db_path) is None


def test_returns_none_when_integration_disabled(db_path: Path) -> None:
    _repo(db_path).set_credential(
        kind="composio", api_key="key", enabled=False
    )
    assert load_composio_credential(db_path) is None


def test_api_key_not_logged(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The decrypted API key must never appear in log output."""
    _repo(db_path).set_credential(kind="composio", api_key="csk-never-log-me")
    import logging  # noqa: PLC0415

    with caplog.at_level(logging.DEBUG):
        load_composio_credential(db_path)

    assert "csk-never-log-me" not in caplog.text
