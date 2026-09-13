"""OAuth stays in Hermes; only discovered account models may be selected."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes.agents_os.infrastructure import dbus_runtime_service as dbus
from hermes.providers.infrastructure import native_model_selection as subject


@pytest.fixture
def setup(tmp_path, monkeypatch):
    active = {
        "provider_id": "openai-codex",
        "default_model": "account-astra",
        "base_url": "",
        "is_active": True,
    }
    wiring = SimpleNamespace(
        _authorize_and_resolve=Mock(),
        _local_llm_db_path=lambda: tmp_path / "runtime.db",
        _active_provider_svc=Mock(),
    )
    monkeypatch.setattr(dbus, "_read_native_active", lambda: dict(active))
    write = Mock(side_effect=lambda _provider, model, _url: active.update(default_model=model))
    monkeypatch.setattr(dbus, "_write_hermes_model_config", write)
    monkeypatch.setattr(dbus, "_remember_native_provider_model", Mock())
    monkeypatch.setattr(dbus, "_clear_engine_runtime_cache", Mock())
    monkeypatch.setattr(
        dbus, "_write_hermes_env", Mock(side_effect=AssertionError("no credential writes"))
    )
    live = Mock(return_value=(["account-astra", "account-terra"], "private-account"))
    monkeypatch.setattr(subject, "_live_models", live)
    monkeypatch.setattr(
        subject, "_same_oauth_account", lambda account: account == "private-account"
    )
    return wiring, active, write, live


def select(wiring, model="account-terra", expected_model="account-astra"):
    return subject.select_model(
        wiring,
        provider_id="openai-codex",
        model=model,
        expected_model=expected_model,
        sender_uid=1000,
    )


def test_catalog_uses_live_ids_without_account_or_credentials(setup):
    wiring, _active, _write, live = setup
    assert subject.list_models(wiring, provider_id="openai-codex", sender_uid=1000) == {
        "provider_id": "openai-codex",
        "active_model": "account-astra",
        "models": ["account-astra", "account-terra"],
    }
    live.assert_called_once()


def test_model_only_write_keeps_provider_endpoint_and_oauth(setup):
    wiring, active, write, live = setup
    active["base_url"] = "https://chatgpt.com/backend-api/codex"
    assert select(wiring) == {"provider_id": "openai-codex", "active_model": "account-terra"}
    write.assert_called_once_with("openai-codex", "account-terra", active["base_url"])
    live.assert_called_once()
    wiring._active_provider_svc.force_refresh.assert_called_once()
    dbus._clear_engine_runtime_cache.assert_called_once()


def test_silent_native_write_failure_is_not_reported_as_success(setup):
    wiring, _active, write, _live = setup
    write.side_effect = None
    assert select(wiring) == {"code": "models_unavailable"}
    wiring._active_provider_svc.force_refresh.assert_not_called()


@pytest.mark.parametrize("model", ["not-in-account", "", "../model", "x" * 129])
def test_never_saves_an_unadvertised_or_invalid_model(setup, model):
    wiring, _active, write, _live = setup
    assert "code" in select(wiring, model)
    write.assert_not_called()


def test_rechecks_active_model_and_account_before_commit(setup, monkeypatch):
    wiring, active, write, live = setup
    assert select(wiring, expected_model="stale-model") == {"code": "selection_changed"}
    live.assert_not_called()
    live.side_effect = lambda: (
        active.update(default_model="other-selection") or ["account-terra"],
        "private-account",
    )
    assert select(wiring) == {"code": "selection_changed"}
    write.assert_not_called()
    active["default_model"] = "account-astra"
    live.side_effect = None
    monkeypatch.setattr(subject, "_same_oauth_account", lambda _account: False)
    assert select(wiring) == {"code": "selection_changed"}
    write.assert_not_called()


def test_preserves_enterprise_policy_applied_during_discovery(setup, monkeypatch):
    wiring, _active, write, _live = setup
    entries = 0

    @contextmanager
    def policy_lock(_path):
        nonlocal entries
        entries += 1
        if entries == 2:
            raise PermissionError("corporate policy arrived")
        yield

    monkeypatch.setattr(subject, "local_configuration_write", policy_lock)
    assert select(wiring) == {"code": "managed_by_enterprise"}
    write.assert_not_called()


def test_unauthorized_bus_sender_cannot_discover_or_mutate(setup):
    wiring, _active, write, live = setup
    wiring._authorize_and_resolve.side_effect = PermissionError("not authorized")
    with pytest.raises(PermissionError):
        select(wiring)
    with pytest.raises(PermissionError):
        subject.list_models(wiring, provider_id="openai-codex", sender_uid=1000)
    live.assert_not_called()
    write.assert_not_called()


def test_discovery_failure_is_safe_and_never_changes_selection(setup):
    wiring, _active, write, live = setup
    live.side_effect = RuntimeError("private-token-account")
    assert select(wiring) == {"code": "models_unavailable"}
    assert subject.list_models(wiring, provider_id="openai-codex", sender_uid=1000) == {
        "code": "models_unavailable"
    }
    write.assert_not_called()


def test_other_providers_are_not_reconfigured_as_codex(setup):
    wiring, active, write, live = setup
    active["provider_id"] = "custom"
    assert select(wiring) == {"code": "selection_changed"}
    assert subject.list_models(wiring, provider_id="custom", sender_uid=1000) == {
        "code": "unsupported_provider"
    }
    write.assert_not_called()
    live.assert_not_called()


def test_live_discovery_uses_native_auth_and_only_visible_raw_account_slugs(monkeypatch):
    import hermes_cli.codex_models as models
    import httpx
    from hermes_cli import auth

    monkeypatch.setattr(
        auth,
        "resolve_codex_runtime_credentials",
        Mock(return_value={"api_key": "private-oauth-token"}),
    )
    monkeypatch.setattr(models, "_extract_chatgpt_account_id", lambda _token: "private-account")
    response = Mock()
    response.json.return_value = {
        "models": [
            {"slug": "actual-account-model", "priority": 2},
            {"slug": "hidden-model", "visibility": "hidden"},
            {"slug": "actual-account-model", "priority": 1},
            {"slug": "bad/model"},
        ]
    }
    get = Mock(return_value=response)
    monkeypatch.setattr(httpx, "get", get)
    assert subject._live_models() == (["actual-account-model"], "private-account")
    args, kwargs = get.call_args
    assert args == ("https://chatgpt.com/backend-api/codex/models?client_version=1.0.0",)
    assert kwargs["headers"]["ChatGPT-Account-Id"] == "private-account"
    assert kwargs["follow_redirects"] is False
    assert kwargs["timeout"] == 10


@pytest.mark.parametrize(
    "entries", [[], None, "malformed", [{"slug": "hidden", "visibility": "hide"}]]
)
def test_empty_or_malformed_account_catalog_has_no_fallback(monkeypatch, entries):
    import hermes_cli.codex_models as models
    import httpx
    from hermes_cli import auth

    monkeypatch.setattr(
        auth, "resolve_codex_runtime_credentials", lambda **_kwargs: {"api_key": "token"}
    )
    monkeypatch.setattr(models, "_extract_chatgpt_account_id", lambda _token: "account")
    response = Mock()
    response.json.return_value = {"models": entries}
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: response)
    with pytest.raises(subject.SelectionUnavailable):
        subject._live_models()
