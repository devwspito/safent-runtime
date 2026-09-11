"""No daemon services or live inference; sealed descriptors and real SQLite."""

import os
import sqlite3
import sys
import threading
import time

import pytest

from hermes.runtime import managed_llm_bootstrap as module
from hermes.runtime.managed_llm import apply_signed_gateway
from hermes.runtime.managed_llm_lifecycle import LifecycleUnavailable, reconcile_generation
from hermes.runtime.managed_llm_profile import (
    create_profile_home,
    daemon_environment,
    write_profile,
)
from tests.unit.test_llm_configuration_lock import _seed

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_process_guard(monkeypatch):
    monkeypatch.setattr(module, "_ADMISSION", None)
    # Other unit suites install fake native modules in this pytest process.
    # Model a fresh pre-import process here; the disposable exec fixture proves
    # the real interpreter boundary. Restore all prior modules on teardown.
    for name in tuple(sys.modules):
        if name == "run_agent" or name.startswith(("agent.", "hermes_cli.")):
            monkeypatch.delitem(sys.modules, name)


def test_local_boot_does_not_reexec_or_release_corporate_permission(tmp_path):
    path = tmp_path / "state.db"
    pending = module.initialize_process(
        path,
        exec_fn=lambda *_: pytest.fail("local exec"),
    )
    assert module.process_admission() is None
    guard = module.complete_process_bootstrap(
        pending, profile_factory=lambda _: pytest.fail("local profile")
    )
    assert guard.check().mode == "local"
    with pytest.raises(LifecycleUnavailable):
        module.assert_process_admission(managed=True)


@pytest.mark.skipif(not hasattr(os, "memfd_create"), reason="Linux daemon sealed bootstrap")
def test_managed_first_stage_exec_uses_same_daemon_without_provider_environment(
    tmp_path, monkeypatch
):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)
    monkeypatch.setenv("OPENAI_API_KEY", "fictional-personal")
    monkeypatch.setenv("HERMES_MODEL", "personal")
    called = []

    def capture(executable, argv, env):
        called.append((executable, argv, env))
        fd = int(argv[-1].split("=")[1])
        assert os.get_inheritable(fd)
        assert "fictional-personal" not in os.read(fd, 4096).decode()
        assert argv[1:3] == ["-m", "hermes.runtime"]
        assert "OPENAI_API_KEY" not in env and "HERMES_MODEL" not in env
        assert env["HOME"] == env["HERMES_HOME"]
        assert env["HERMES_SHELL_DB"] == str(path)

    with pytest.raises(LifecycleUnavailable, match="unexpectedly returned"):
        module.initialize_process(
            path,
            argv=["--systemd-notify"],
            exec_fn=capture,
        )
    assert len(called) == 1
    assert reconcile_generation(path).restart_pending
    assert module.process_admission() is None


@pytest.mark.skipif(not hasattr(os, "memfd_create"), reason="Linux daemon sealed bootstrap")
def test_sealed_receipt_checks_generation_env_and_profile_before_ack(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)
    generation = reconcile_generation(path)
    profile = create_profile_home(path, generation.generation)
    fd = module._receipt_fd(generation, profile)
    clean = daemon_environment({}, profile)
    monkeypatch.setattr(os, "environ", clean)
    pending = module.initialize_process(
        path,
        argv=[f"--managed-bootstrap-fd={fd}"],
    )
    assert module.process_admission() is None
    assert not (profile / "config.yaml").exists()
    assert reconcile_generation(path).restart_pending
    guard = module.complete_process_bootstrap(
        pending,
        profile_factory=lambda _: {"model": {}, "mcp_servers": {}},
    )
    assert guard.check().mode == "managed"
    assert (profile / "config.yaml").exists()
    assert not reconcile_generation(path).restart_pending


@pytest.mark.skipif(not hasattr(os, "memfd_create"), reason="Linux daemon sealed bootstrap")
@pytest.mark.parametrize("attack", ["env", "changed", "unsealed", "used_profile", "native_import"])
def test_bootstrap_rejects_unsafe_receipts_without_ack(tmp_path, monkeypatch, attack):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)
    generation = reconcile_generation(path)
    profile = create_profile_home(path, generation.generation)
    fd = module._receipt_fd(generation, profile)
    clean = daemon_environment({}, profile)
    monkeypatch.setattr(os, "environ", clean)
    if attack == "env":
        clean["OPENAI_API_KEY"] = "fictional-injected"
    elif attack == "changed":
        wiring._association_store.mark_revoked()
    elif attack == "used_profile":
        (profile / ".env").touch()
    elif attack == "native_import":
        from types import ModuleType

        monkeypatch.setitem(sys.modules, "agent.auxiliary_client", ModuleType("fixture-native"))
    else:
        os.close(fd)
        fd = os.open(tmp_path / "fake", os.O_CREAT | os.O_RDWR, 0o600)
        os.write(fd, b"{}")
        os.lseek(fd, 0, os.SEEK_SET)
    with pytest.raises(LifecycleUnavailable):
        module.initialize_process(
            path,
            argv=[f"--managed-bootstrap-fd={fd}"],
        )
    assert reconcile_generation(path).restart_pending
    assert module.process_admission() is None


def test_profile_writes_never_overwrite_existing_data_or_follow_links(tmp_path):
    profile = create_profile_home(tmp_path / "state.db", 1)
    write_profile(profile, {"model": {}})
    first = (profile / "config.yaml").read_bytes()
    with pytest.raises(FileExistsError):
        write_profile(profile, {"model": {"default": "unexpected"}})
    assert (profile / "config.yaml").read_bytes() == first
    linked = tmp_path / "linked"
    linked.symlink_to(profile, target_is_directory=True)
    with pytest.raises(PermissionError):
        write_profile(linked, {})


def test_guard_checks_policy_none_before_personal_fallback(tmp_path):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    guard = module.complete_process_bootstrap(
        module.initialize_process(path), profile_factory=lambda _: {}
    )
    apply_signed_gateway(wiring, signed)
    wiring._association_store.clear()
    assert reconcile_generation(path).mode == "local"
    with pytest.raises(LifecycleUnavailable):
        module.assert_process_admission(path)
    with pytest.raises(LifecycleUnavailable):
        guard.check()


def test_blocked_boot_can_stay_idle_without_restarting_on_denied_admission(tmp_path):
    from hermes.runtime.managed_llm_lifecycle import ProcessAdmission, acknowledge_boot

    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)
    wiring._association_store.mark_revoked()
    guard = ProcessAdmission(path, acknowledge_boot(path, reconcile_generation(path)))
    for _ in range(2):
        assert guard.check(allow_blocked=True).mode == "blocked"
        with pytest.raises(LifecycleUnavailable):
            guard.check()


def test_failed_generation_write_rolls_back_association_revocation(tmp_path, monkeypatch):
    from hermes.runtime import managed_llm_lifecycle as lifecycle

    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)

    def fail(_):
        raise sqlite3.OperationalError("fixture full disk")

    monkeypatch.setattr(lifecycle, "record_authority_transition", fail)
    with pytest.raises(sqlite3.OperationalError):
        wiring._association_store.mark_revoked()
    assert wiring._association_store.get().state == "active"


@pytest.mark.asyncio
async def test_confinement_failure_never_calls_profile_factory_or_admits(tmp_path, monkeypatch):
    from hermes import logging_setup
    from hermes.runtime import __main__ as daemon

    path = tmp_path / "state.db"
    pending = module.initialize_process(path)
    monkeypatch.setattr(logging_setup, "configure_structured_logging", lambda **_: None)
    monkeypatch.setattr(daemon, "_ensure_state_db_secure", lambda: None)

    def denied():
        raise PermissionError("fixture confinement denied")

    monkeypatch.setattr(daemon, "_apply_runtime_landlock", denied)
    monkeypatch.setattr(
        module,
        "complete_process_bootstrap",
        lambda *_args, **_kwargs: pytest.fail("bootstrap before jail"),
    )
    with pytest.raises(PermissionError, match="confinement denied"):
        await daemon._run(systemd_notify=False, bootstrap=pending)
    assert module.process_admission() is None
    assert reconcile_generation(path).restart_pending


@pytest.mark.skipif(not hasattr(os, "memfd_create"), reason="Linux daemon sealed bootstrap")
def test_prejail_bootstrap_never_decrypts_vault_and_factory_failure_stays_pending(
    tmp_path, monkeypatch
):
    from hermes.shell_server.security.secrets import SecretsVault

    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)
    generation = reconcile_generation(path)
    profile = create_profile_home(path, generation.generation)
    fd = module._receipt_fd(generation, profile)
    monkeypatch.setattr(os, "environ", daemon_environment({}, profile))
    monkeypatch.setattr(
        SecretsVault,
        "decrypt",
        lambda *_args, **_kwargs: pytest.fail("vault accessed before confinement"),
    )
    pending = module.initialize_process(path, argv=[f"--managed-bootstrap-fd={fd}"])
    assert module.process_admission() is None

    def failed_factory(_):
        raise RuntimeError("fixture native configuration unavailable")

    with pytest.raises(RuntimeError, match="configuration unavailable"):
        module.complete_process_bootstrap(pending, profile_factory=failed_factory)
    assert module.process_admission() is None
    assert reconcile_generation(path).restart_pending
    assert not (profile / "config.yaml").exists()


def test_identity_mismatch_closes_immediately_without_running_stuck_interrupt(tmp_path):
    pending = module.initialize_process(tmp_path / "state.db")
    guard = module.complete_process_bootstrap(pending, profile_factory=lambda _: {})
    called = threading.Event()

    def stuck():
        called.set()
        threading.Event().wait(2)

    guard.register_interrupt(stuck)
    before = time.monotonic()
    with pytest.raises(LifecycleUnavailable):
        module.assert_process_admission(managed=True)
    assert time.monotonic() - before < 0.2
    assert not called.is_set()
    with pytest.raises(LifecycleUnavailable):
        guard.check()
