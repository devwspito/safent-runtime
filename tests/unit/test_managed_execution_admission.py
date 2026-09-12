"""Admission checks against real signed bindings, vault and lifecycle generation."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from hermes.runtime import managed_llm_bootstrap as bootstrap
from hermes.runtime.managed_llm import resolve_managed_config
from hermes.runtime.managed_llm_lifecycle import (
    ProcessAdmission,
    acknowledge_boot,
    reconcile_generation,
)
from hermes.runtime.model_config import ManagedProviderUnavailableError, ModelConfig
from hermes.runtime.nous_engine import _assert_managed_execution_ready
from tests.unit import test_managed_llm_gateway as gateway_fixture

pytestmark = pytest.mark.unit
setup = gateway_fixture.setup


@pytest.fixture(autouse=True)
def isolated_guard(monkeypatch):
    monkeypatch.setattr(bootstrap, "_ADMISSION", None)


def admit(path, monkeypatch):
    # Unit-level lifecycle admission; the native integration fixture performs
    # the sealed-descriptor clean exec and complete_process_bootstrap itself.
    guard = ProcessAdmission(path, acknowledge_boot(path, reconcile_generation(path)))
    monkeypatch.setattr(bootstrap, "_ADMISSION", guard)
    return guard


def test_signed_assignment_without_boot_never_releases_key(setup):
    wiring, bundle = setup
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    with patch(
        "hermes.shell_server.providers.repo.SQLiteProviderRepository.reveal_api_key"
    ) as reveal:
        with pytest.raises(ManagedProviderUnavailableError, match="bootstrap"):
            resolve_managed_config(wiring._provider_repo._db_path)
        reveal.assert_not_called()


def test_admitted_managed_binding_and_factory_gate_are_functional(setup, monkeypatch):
    wiring, bundle = setup
    path = wiring._provider_repo._db_path
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    admit(path, monkeypatch)
    config = resolve_managed_config(path)
    assert config.managed
    _assert_managed_execution_ready(replace(config, max_tokens=16, temperature=0.2))


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "custom/other"),
        ("api_key", "fictional-other"),
        ("base_url", "https://other.invalid/v1"),
        ("native_provider", "openai"),
        ("managed", False),
    ],
)
def test_factory_rejects_caller_selected_binding(setup, monkeypatch, field, value):
    wiring, bundle = setup
    path = wiring._provider_repo._db_path
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    admit(path, monkeypatch)
    config = resolve_managed_config(path)
    with pytest.raises(ManagedProviderUnavailableError, match="assignment"):
        _assert_managed_execution_ready(replace(config, **{field: value}))


def test_unknown_alias_cannot_use_the_active_binding(setup, monkeypatch):
    wiring, bundle = setup
    path = wiring._provider_repo._db_path
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    admit(path, monkeypatch)
    with pytest.raises(ManagedProviderUnavailableError, match="not assigned"):
        resolve_managed_config(path, "personal")


@pytest.mark.parametrize("change", ["revoke", "new_generation", "unpair", "pid", "closed"])
def test_old_process_cannot_release_or_use_binding(setup, monkeypatch, change):
    wiring, bundle = setup
    path = wiring._provider_repo._db_path
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    guard = admit(path, monkeypatch)
    config = resolve_managed_config(path)
    if change == "revoke":
        wiring.apply_managed_llm_gateway(bundle_json=bundle(2, providers=False), sender_uid=1000)
    elif change == "new_generation":
        wiring.apply_managed_llm_gateway(bundle_json=bundle(2), sender_uid=1000)
    elif change == "unpair":
        wiring._association_store.clear()
    elif change == "pid":
        monkeypatch.setattr("hermes.runtime.managed_llm_lifecycle.os.getpid", lambda: -1)
    else:
        guard.close()
    for action in (
        lambda: resolve_managed_config(path),
        lambda: _assert_managed_execution_ready(config),
    ):
        with pytest.raises(ManagedProviderUnavailableError):
            action()


def test_local_process_and_env_flags_cannot_admit_managed(setup, monkeypatch):
    wiring, bundle = setup
    path = wiring._provider_repo._db_path
    admit(path, monkeypatch)
    monkeypatch.setenv("HERMES_MANAGED_EXECUTION_READY", "1")
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    with pytest.raises(ManagedProviderUnavailableError):
        resolve_managed_config(path)


def test_personal_unpaired_process_keeps_native_resolution(tmp_path, monkeypatch):
    path = tmp_path / "local.db"
    admit(path, monkeypatch)
    assert resolve_managed_config(path) is None
    with pytest.raises(ManagedProviderUnavailableError):
        _assert_managed_execution_ready(ModelConfig(model="custom/company", managed=True))


def test_other_database_cannot_borrow_an_admitted_process(setup, monkeypatch, tmp_path):
    wiring, bundle = setup
    path = wiring._provider_repo._db_path
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    admit(path, monkeypatch)
    with pytest.raises(ManagedProviderUnavailableError):
        resolve_managed_config(tmp_path / "other.db")
    with pytest.raises(ManagedProviderUnavailableError):
        resolve_managed_config(path)


def test_authority_changed_during_binding_read_never_releases_snapshot(setup, monkeypatch):
    from hermes.runtime import managed_llm

    wiring, bundle = setup
    path = wiring._provider_repo._db_path
    wiring.apply_managed_llm_gateway(bundle_json=bundle(), sender_uid=1000)
    admit(path, monkeypatch)
    original = managed_llm._resolve_managed_binding

    def changed(*args):
        config = original(*args)
        wiring.apply_managed_llm_gateway(bundle_json=bundle(2), sender_uid=1000)
        return config

    monkeypatch.setattr(managed_llm, "_resolve_managed_binding", changed)
    with pytest.raises(ManagedProviderUnavailableError):
        resolve_managed_config(path)
