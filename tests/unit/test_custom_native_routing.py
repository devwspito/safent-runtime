"""A custom Qwen endpoint never inherits the official Codex wire protocol/key."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes.agents_os.infrastructure import dbus_runtime_service as dbus
from hermes.providers.infrastructure.nous_provider_adapter import (
    nous_request_from_model_config,
    nous_request_from_resolved,
)
from hermes.runtime.model_config import ModelConfig
from hermes.runtime.provider_config_source import _load_native_model_config
from hermes.shell_server.providers.domain import ProviderKind
from hermes.shell_server.providers.native_sync import (
    kind_to_native_target,
    native_provider_for_endpoint,
)

CUSTOM_URL = "https://model-owner.example/v1"


@pytest.mark.parametrize("provider", ["openai", "openai-api"])
@pytest.mark.parametrize(
    "url", [CUSTOM_URL, "http://127.0.0.1:8000/v1", "https://api.openai.com.attacker.invalid/v1"]
)
def test_custom_endpoint_overrides_ambiguous_official_slug(provider, url):
    assert native_provider_for_endpoint(provider, url) == "custom"
    request, bare = nous_request_from_model_config(
        ModelConfig(model=f"{provider}/qwen-local", base_url=url, api_key="owner-key")
    )
    assert request.requested == "custom"
    assert request.explicit_base_url == url
    assert request.explicit_api_key == "owner-key"
    assert bare == "qwen-local"


@pytest.mark.parametrize("url", [None, "https://api.openai.com/v1", "https://eu.api.openai.com/v1"])
def test_official_openai_and_codex_remain_native(url):
    assert native_provider_for_endpoint("openai-api", url) == "openai-api"
    assert native_provider_for_endpoint("openai-codex", url) == "openai-codex"


def test_kind_mapping_has_endpoint_context():
    assert kind_to_native_target(ProviderKind.VLLM, base_url=CUSTOM_URL).provider_id == "custom"
    assert kind_to_native_target(ProviderKind.OPENAI, base_url=CUSTOM_URL).provider_id == "custom"
    assert (
        kind_to_native_target(ProviderKind.ANTHROPIC, base_url=CUSTOM_URL).provider_id
        == "anthropic"
    )


def test_resolved_openai_compatible_custom_endpoint_stays_custom():
    from hermes.providers.domain.catalog import canonical_for

    resolved = SimpleNamespace(
        canonical=canonical_for(ProviderKind.OPENAI), base_url=CUSTOM_URL, api_key="owner-key"
    )
    request = nous_request_from_resolved(resolved, target_model="qwen-local")
    assert request.requested == "custom"
    assert request.explicit_api_key == "owner-key"


def test_sql_activation_uses_native_custom_key_not_official_environment(monkeypatch):
    from hermes_cli import auth

    monkeypatch.setattr(auth, "PROVIDER_REGISTRY", {})
    write = Mock()
    env = Mock()
    monkeypatch.setattr(dbus, "_write_hermes_model_config", write)
    monkeypatch.setattr(dbus, "_write_hermes_env", env)
    monkeypatch.setattr(dbus, "_clear_engine_runtime_cache", Mock())
    wiring = SimpleNamespace(_active_provider_svc=None)
    provider = SimpleNamespace(
        kind=ProviderKind.VLLM, base_url=CUSTOM_URL, default_model="qwen-local", managed_by=None
    )
    dbus.DbusRuntimeServiceWiring._sync_to_native_provider(
        wiring, provider, "custom-owner-key", set_active=True
    )
    write.assert_called_once_with("custom", "qwen-local", CUSTOM_URL, api_key="custom-owner-key")
    env.assert_not_called()


def test_custom_snapshot_passes_its_explicit_key_and_legacy_selection_normalizes(monkeypatch):
    from hermes_cli import auth, config

    cfg = {
        "model": {
            "provider": "custom",
            "default": "qwen-local",
            "base_url": CUSTOM_URL,
            "api_key": "custom-owner-key",
        }
    }
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(
        auth,
        "PROVIDER_REGISTRY",
        {"openai-api": SimpleNamespace(api_key_env_vars=("OPENAI_API_KEY",))},
    )
    monkeypatch.setenv("OPENAI_API_KEY", "previous-official-key")
    model = _load_native_model_config()
    assert model.native_provider == "custom"
    assert model.api_key == "custom-owner-key"
    cfg["model"] = {"provider": "openai-api", "default": "qwen-local", "base_url": CUSTOM_URL}
    model = _load_native_model_config()
    assert model.native_provider == "custom"
    assert model.model == "custom/qwen-local"
    assert model.api_key == "previous-official-key"


def test_switch_clears_previous_transport_and_inline_secret(monkeypatch):
    from hermes_cli import config

    cfg = {
        "model": {
            "provider": "openai-codex",
            "default": "old",
            "api_mode": "codex_responses",
            "api_key": "stale",
            "temperature": 0,
        }
    }
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(config, "save_config", cfg.update)
    dbus._write_hermes_model_config("custom", "qwen-local", CUSTOM_URL, api_key="custom-owner-key")
    assert "api_mode" not in cfg["model"]
    assert cfg["model"]["api_key"] == "custom-owner-key"
    assert cfg["model"]["temperature"] == 0
    dbus._write_hermes_model_config("openai-codex", "account-terra")
    assert "api_key" not in cfg["model"]
    assert "api_mode" not in cfg["model"]


def test_real_native_resolver_uses_chat_completions_for_custom_qwen(monkeypatch):
    from hermes_cli import runtime_provider

    monkeypatch.setattr(
        runtime_provider,
        "_get_model_config",
        lambda: {"provider": "openai-codex", "api_mode": "codex_responses"},
    )
    monkeypatch.setattr(
        runtime_provider, "_try_resolve_from_custom_pool", lambda *_args, **_kwargs: None
    )
    result = runtime_provider.resolve_runtime_provider(
        requested="custom",
        explicit_api_key="custom-owner-key",
        explicit_base_url=CUSTOM_URL,
        target_model="qwen-local",
    )
    assert result["provider"] == "custom"
    assert result["api_mode"] == "chat_completions"
    assert result["api_key"] == "custom-owner-key"
