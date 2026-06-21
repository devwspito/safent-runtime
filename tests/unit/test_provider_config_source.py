"""El runtime construye ModelConfig desde el PROVIDER ACTIVO (onboarding/Settings).

Cierra el hueco: la UI persistía el provider en la tabla `providers` pero el daemon
lo ignoraba (env-frozen). Ahora `resolve_model_config` lee el provider activo +
descifra la api_key con el MISMO SecretsVault, y cae a env si no hay provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.runtime.provider_config_source import (
    load_active_model_config,
    resolve_model_config,
)
from hermes.shell_server.providers.domain import ProviderKind, new_provider
from hermes.shell_server.providers.repo import SQLiteProviderRepository
from hermes.shell_server.security import secrets as secrets_mod
from hermes.shell_server.security.secrets import SecretsVault

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def master_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """master.key de prueba en tmp; el SecretsVault() sin arg la lee (mismo key
    para escribir el provider y para que el runtime lo descifre). autouse: todo
    test del módulo necesita la master key parcheada."""
    key_file = tmp_path / "master.key"
    key_file.write_bytes(b"\x11" * 32)
    monkeypatch.setattr(secrets_mod, "_MASTER_KEY_PATH", key_file)
    return key_file


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "shell-state.db"


def _repo(db_path: Path) -> SQLiteProviderRepository:
    return SQLiteProviderRepository(db_path=db_path, vault=SecretsVault())


def test_active_openai_provider_becomes_model_config(
    db_path: Path
) -> None:
    repo = _repo(db_path)
    provider = new_provider(
        alias="OpenAI prod",
        kind=ProviderKind.OPENAI,
        default_model="gpt-5.4-nano",
        has_api_key=True,
    )
    repo.add(provider=provider, api_key="sk-secret-xyz")
    repo.set_active(provider_id=provider.provider_id)

    config = load_active_model_config(db_path)

    assert config is not None
    assert config.model == "openai/gpt-5.4-nano"  # litellm_model_string aplicado
    assert config.api_key == "sk-secret-xyz"  # descifrada con el mismo vault
    assert config.base_url is None


def test_active_vllm_provider_keeps_base_url_and_prefix(
    db_path: Path
) -> None:
    repo = _repo(db_path)
    provider = new_provider(
        alias="vLLM local",
        kind=ProviderKind.VLLM,
        default_model="qwen3.6-35b-a3b",
        base_url="http://127.0.0.1:8888/v1",
        has_api_key=False,
    )
    repo.add(provider=provider, api_key=None)
    repo.set_active(provider_id=provider.provider_id)

    config = load_active_model_config(db_path)

    assert config is not None
    assert config.model == "hosted_vllm/qwen3.6-35b-a3b"
    assert config.base_url == "http://127.0.0.1:8888/v1"
    assert config.api_key is None


def test_no_active_provider_returns_none(db_path: Path) -> None:
    repo = _repo(db_path)
    # provider creado pero NO activo
    provider = new_provider(
        alias="idle", kind=ProviderKind.OPENAI, default_model="gpt-4o"
    )
    repo.add(provider=provider, api_key=None)
    assert load_active_model_config(db_path) is None


def test_missing_db_is_fail_soft_none(tmp_path: Path) -> None:
    # DB inexistente -> None (fail-soft), no excepción.
    assert load_active_model_config(tmp_path / "nope.db") is None


def test_resolve_falls_back_to_env_when_no_provider(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_MODEL", "anthropic/claude-3-5-haiku-20241022")
    config = resolve_model_config(db_path)  # sin provider activo
    assert config is not None
    assert config.model == "anthropic/claude-3-5-haiku-20241022"


def test_resolve_prefers_active_provider_over_env(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_MODEL", "anthropic/claude-3-5-haiku-20241022")
    repo = _repo(db_path)
    provider = new_provider(
        alias="prefer-me", kind=ProviderKind.OPENAI, default_model="gpt-5.4-nano"
    )
    repo.add(provider=provider, api_key=None)
    repo.set_active(provider_id=provider.provider_id)

    config = resolve_model_config(db_path)
    assert config is not None
    assert config.model == "openai/gpt-5.4-nano"  # provider gana al env


def test_resolve_returns_none_when_neither(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    assert resolve_model_config(db_path) is None
