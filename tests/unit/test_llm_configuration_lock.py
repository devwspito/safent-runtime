"""Cross-process local/managed configuration ordering, with fictional secrets."""

import os
import stat
import time
from dataclasses import replace
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes.config_sync.policy_document import (
    PolicyBundle,
    PolicyPayload,
    ProviderSpec,
    signing_bytes,
)
from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore
from hermes.runtime.managed_llm import local_configuration_write
from hermes.security.configuration_lock import ConfigurationLockError, configuration_lock
from hermes.shell_server.providers.repo import SQLiteProviderRepository
from hermes.shell_server.security.secrets import SecretsVault

pytestmark = pytest.mark.unit
_KEY = b"L" * 32


def _wiring(path):
    vault = SecretsVault(master_key=_KEY)
    repo = SQLiteProviderRepository(db_path=Path(path), vault=vault)
    store = SQLiteAssociationStore(db_path=Path(path), vault=vault)
    return SimpleNamespace(_provider_repo=repo, _association_store=store, _active_provider_svc=None)


def _seed(path):
    wiring = _wiring(path)
    private = Ed25519PrivateKey.generate()
    now = datetime.now(UTC).isoformat()
    wiring._association_store.save(
        association=InstanceAssociation(
            instance_id="instance-lock-test",
            tenant_id="org-lock-test",
            paired_at=now,
            cloud_endpoint="https://enterprise.example",
            signing_pubkey_hex=private.public_key().public_bytes_raw().hex(),
            license={},
            last_applied_version=0,
            state="active",
        ),
        instance_secret="fictional-pairing-token",
    )
    payload = PolicyPayload(
        llm_instance_id="instance-lock-test",
        providers=[
            ProviderSpec(
                alias="company",
                kind="openai_compatible",
                default_model="test-model",
                credential_kind="instance_gateway",
                api_key="fictional-inference-token",
                set_active=True,
                base_url="https://enterprise.example/v1/inference/test-grant/v1",
            )
        ],
    )
    data = dict(version=1, tenant_id="org-lock-test", issued_at=now, payload=payload)
    signed = PolicyBundle(
        **data, signature_hex=private.sign(signing_bytes(**data)).hex()
    ).model_dump_json()
    return wiring, signed


def _local_writer(path, started, entered):
    started.set()
    with local_configuration_write(Path(path)):
        entered.set()


def _paused_apply(path, signed, verifying, release, outcome):
    from hermes.config_sync import signature
    from hermes.runtime.managed_llm import apply_signed_gateway

    original = signature.verify_bundle

    def pause(**kwargs):
        result = original(**kwargs)
        verifying.set()
        assert release.wait(timeout=8)
        return result

    signature.verify_bundle = pause
    try:
        apply_signed_gateway(_wiring(path), signed)
        outcome.put("applied")
    except PermissionError:
        outcome.put("rejected")


def _clear(path, started, finished, mutation="clear"):
    started.set()
    store = _wiring(path)._association_store
    if mutation == "clear":
        store.clear()
    elif mutation == "revoke":
        store.mark_revoked()
    else:
        store.save(
            association=replace(
                store.get(),
                signing_pubkey_hex=Ed25519PrivateKey.generate()
                .public_key()
                .public_bytes_raw()
                .hex(),
            ),
            instance_secret="rotated-test-token",
        )
    finished.set()


def test_local_configuration_guard_serializes_separate_processes(tmp_path):
    path = tmp_path / "state.db"
    context = get_context("spawn")
    started, entered = context.Event(), context.Event()
    worker = context.Process(target=_local_writer, args=(str(path), started, entered))
    try:
        with local_configuration_write(path):
            worker.start()
            assert started.wait(timeout=5)
            assert not entered.wait(timeout=0.3), (
                "second process entered a supposedly exclusive local credential commit"
            )
        worker.join(timeout=8)
        assert worker.exitcode == 0 and entered.is_set()
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=3)


@pytest.mark.parametrize("mutation", ["clear", "revoke", "key_rotation"])
def test_unpair_cannot_finish_mid_signature_then_be_undone_by_old_apply(tmp_path, mutation):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    context = get_context("spawn")
    verifying, release, started, finished = (context.Event() for _ in range(4))
    outcome = context.Queue()
    apply = context.Process(
        target=_paused_apply, args=(str(path), signed, verifying, release, outcome)
    )
    clear = context.Process(target=_clear, args=(str(path), started, finished, mutation))
    try:
        apply.start()
        assert verifying.wait(timeout=8)
        clear.start()
        assert started.wait(timeout=5)
        assert not finished.wait(timeout=0.3), (
            "unpair completed while an old signed snapshot was still applying"
        )
    finally:
        release.set()
        for worker in (apply, clear):
            if worker.pid is not None:
                worker.join(timeout=8)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=3)
    assert apply.exitcode == clear.exitcode == 0
    assert outcome.get(timeout=2) == "applied"
    if mutation == "clear":
        assert wiring._association_store.get() is None
        assert wiring._provider_repo.list_all() == []
    else:
        from hermes.runtime.managed_llm import apply_signed_gateway

        with pytest.raises(PermissionError):
            apply_signed_gateway(wiring, signed)


def _timed_lock(path, output, timeout=0.15):
    started = time.monotonic()
    try:
        with configuration_lock(Path(path), timeout=timeout):
            output.put(("entered", time.monotonic() - started))
    except ConfigurationLockError:
        output.put(("blocked", time.monotonic() - started))


def _crash_lock(path, acquired):
    with configuration_lock(Path(path)):
        acquired.set()
        os._exit(19)


def _late_local_commit(path, started, output):
    started.set()
    try:
        with local_configuration_write(Path(path)):
            output.put("wrote")
    except PermissionError:
        output.put("denied")


def test_timeout_is_bounded_and_a_crash_does_not_leave_stale_lock(tmp_path):
    path = tmp_path / "state.db"
    context = get_context("spawn")
    output = context.Queue()
    with configuration_lock(path):
        worker = context.Process(target=_timed_lock, args=(str(path), output))
        worker.start()
        worker.join(timeout=5)
        assert worker.exitcode == 0
        outcome, elapsed = output.get(timeout=2)
        assert outcome == "blocked" and elapsed < 1
    lock_path = path.with_name(path.name + ".configuration.lock")
    inode = lock_path.stat().st_ino
    acquired = context.Event()
    crashed = context.Process(target=_crash_lock, args=(str(path), acquired))
    crashed.start()
    assert acquired.wait(timeout=5)
    crashed.join(timeout=5)
    assert crashed.exitcode == 19
    with configuration_lock(path, timeout=0.2):
        assert lock_path.stat().st_ino == inode
        assert lock_path.read_bytes() == b""


def test_reentry_spans_association_sqlite_writes_without_deadlock(tmp_path):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    from hermes.runtime.managed_llm import apply_signed_gateway

    with configuration_lock(path):  # noqa: SIM117 - outer lock must survive inner release
        with configuration_lock(path, timeout=0):
            wiring._association_store.update_license({"plan": "test"})
            wiring._association_store.update_directory({"entries": []})
            wiring._association_store.set_last_applied_version(1)
            wiring._association_store.set_last_applied_version(0)
            assert wiring._association_store.get().last_applied_version == 1
            assert apply_signed_gateway(wiring, signed)["status"] == "active"
        wiring._association_store.clear()
    assert wiring._association_store.get() is None


def test_body_errors_are_not_misreported_as_lock_failures(tmp_path):
    with (
        pytest.raises(PermissionError, match="managed by Enterprise"),
        configuration_lock(tmp_path / "db"),
    ):
        raise PermissionError("managed by Enterprise")
    with configuration_lock(tmp_path / "db", timeout=0):
        pass


def test_late_oauth_local_commit_cannot_write_after_signed_assignment(tmp_path):
    from hermes.runtime.managed_llm import apply_signed_gateway

    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    context = get_context("spawn")
    started, output = context.Event(), context.Queue()
    worker = context.Process(target=_late_local_commit, args=(str(path), started, output))
    with configuration_lock(path):
        worker.start()
        assert started.wait(timeout=5)
        apply_signed_gateway(wiring, signed)
    worker.join(timeout=6)
    assert worker.exitcode == 0
    assert output.get(timeout=2) == "denied"


@pytest.mark.parametrize("mutation", ["clear", "revoke", "key_rotation", "tenant", "endpoint"])
def test_signed_apply_validates_current_association_not_a_previous_snapshot(tmp_path, mutation):
    from hermes.runtime.managed_llm import apply_signed_gateway

    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    store = wiring._association_store
    old = store.get()
    with configuration_lock(path):
        if mutation == "clear":
            store.clear()
        elif mutation == "revoke":
            store.mark_revoked()
        else:
            changes = {
                "key_rotation": {
                    "signing_pubkey_hex": Ed25519PrivateKey.generate()
                    .public_key()
                    .public_bytes_raw()
                    .hex()
                },
                "tenant": {"tenant_id": "another-org"},
                "endpoint": {"cloud_endpoint": "https://new-enterprise.example"},
            }[mutation]
            store.save(
                association=replace(old, **changes), instance_secret="replacement-test-token"
            )
    with pytest.raises(PermissionError):
        apply_signed_gateway(wiring, signed)
    assert wiring._provider_repo.list_all() == []


def test_fork_reset_never_unlocks_parents_kernel_lock(tmp_path):
    if "fork" not in __import__("multiprocessing").get_all_start_methods():
        pytest.skip("POSIX fork contract")
    path = tmp_path / "state.db"
    context = get_context("fork")
    output = context.Queue()
    with configuration_lock(path):
        worker = context.Process(target=_timed_lock, args=(str(path), output))
        worker.start()
        worker.join(timeout=5)
        assert worker.exitcode == 0
        assert output.get(timeout=2)[0] == "blocked"
        # A second independent process must still see the parent's held lock.
        spawn = get_context("spawn")
        output2 = spawn.Queue()
        second = spawn.Process(target=_timed_lock, args=(str(path), output2))
        second.start()
        second.join(timeout=5)
        assert second.exitcode == 0
        assert output2.get(timeout=2)[0] == "blocked"
    with configuration_lock(path, timeout=0):
        pass


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "permissions", "contents", "fifo"])
def test_unsafe_lock_files_fail_closed_without_modifying_target(tmp_path, unsafe):
    path = tmp_path / "state.db"
    lock = path.with_name(path.name + ".configuration.lock")
    target = tmp_path / "untouched"
    if unsafe == "symlink":
        target.write_bytes(b"fictitious-private-data")
        lock.symlink_to(target)
    elif unsafe == "hardlink":
        target.touch(mode=0o600)
        os.link(target, lock)
    elif unsafe == "fifo":
        os.mkfifo(lock, 0o600)
    else:
        lock.touch(mode=0o600)
        if unsafe == "permissions":
            lock.chmod(0o644)
        else:
            lock.write_bytes(b"never truncate existing data")
    before = target.read_bytes() if target.exists() else None
    started = time.monotonic()
    with pytest.raises(ConfigurationLockError), configuration_lock(path, timeout=0.1):
        pytest.fail("unsafe lock entered")
    assert time.monotonic() - started < 1
    if before is not None:
        assert target.read_bytes() == before


def test_directory_and_database_aliases_are_fail_closed(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "state.db"
    private.chmod(0o770)
    try:
        with pytest.raises(ConfigurationLockError), configuration_lock(path):
            pytest.fail("unsafe directory entered")
    finally:
        private.chmod(0o700)
    original = tmp_path / "original.db"
    original.touch()
    path.symlink_to(original)
    with pytest.raises(ConfigurationLockError), configuration_lock(path):
        pytest.fail("DB symlink accepted")
    path.unlink()
    os.link(original, path)
    with pytest.raises(ConfigurationLockError), configuration_lock(path):
        pytest.fail("DB hardlink accepted")


def test_safe_empty_file_mode_and_parent_alias_reentry(tmp_path):
    path = tmp_path / "state.db"
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with configuration_lock(path), configuration_lock(alias / "state.db", timeout=0):
        lock = path.with_name(path.name + ".configuration.lock")
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        assert lock.stat().st_nlink == 1 and lock.stat().st_size == 0


def test_unsupported_locking_never_degrades_to_thread_only(tmp_path, monkeypatch):
    from hermes.security import configuration_lock as module

    monkeypatch.setattr(module, "fcntl", None)
    with pytest.raises(ConfigurationLockError), module.configuration_lock(tmp_path / "state.db"):
        pytest.fail("unsupported locking entered")


def test_signed_apply_rejects_separate_provider_database(tmp_path):
    from hermes.runtime.managed_llm import apply_signed_gateway

    wiring, signed = _seed(tmp_path / "association.db")
    wiring._provider_repo = _wiring(tmp_path / "other.db")._provider_repo
    with pytest.raises(PermissionError, match="one scope"):
        apply_signed_gateway(wiring, signed)
    assert wiring._provider_repo.list_all() == []
