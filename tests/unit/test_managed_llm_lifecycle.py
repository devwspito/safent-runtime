"""Lifecycle authority uses real SQLite and fictional signed associations."""

import asyncio
import json
import sqlite3
import threading
import time
from dataclasses import replace

import pytest

from hermes.runtime.managed_llm import apply_signed_gateway
from hermes.runtime.managed_llm_lifecycle import (
    LifecycleUnavailable,
    ProcessAdmission,
    acknowledge_boot,
    reconcile_generation,
)
from tests.unit.test_llm_configuration_lock import _seed

pytestmark = pytest.mark.unit


def boot(path):
    return ProcessAdmission(path, acknowledge_boot(path, reconcile_generation(path)))


def test_local_process_cannot_admit_after_managed_assignment(tmp_path):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    old = boot(path)
    assert old.check().mode == "local"
    apply_signed_gateway(wiring, signed)
    with pytest.raises(LifecycleUnavailable):
        old.check()
    assert reconcile_generation(path).restart_pending
    # This is generation evidence only; production inference remains gated.
    assert boot(path).check().mode == "managed"


@pytest.mark.parametrize("mutation", ["clear", "revoke", "key"])
def test_old_managed_process_latches_closed_after_authority_change(tmp_path, mutation):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)
    old = boot(path)
    store = wiring._association_store
    original = store.get()
    if mutation == "clear":
        store.clear()
    elif mutation == "revoke":
        store.mark_revoked()
    else:
        store.save(
            association=replace(original, signing_pubkey_hex="different-key"),
            instance_secret="fictional-new-secret",
        )
    with pytest.raises(LifecycleUnavailable):
        old.check()
    store.save(association=original, instance_secret="fictional-restored-secret")
    if mutation == "clear":
        apply_signed_gateway(wiring, signed)
    boot(path)
    with pytest.raises(LifecycleUnavailable):
        old.check()


def test_transition_away_and_back_between_polls_still_invalidates_process(tmp_path):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)
    old = boot(path)
    store = wiring._association_store
    original = store.get()
    store.mark_revoked()
    store.save(association=original, instance_secret="fictional-restored-secret")
    assert reconcile_generation(path).fingerprint == old.boot.fingerprint
    assert reconcile_generation(path).generation > old.boot.generation
    with pytest.raises(LifecycleUnavailable):
        old.check()


def test_signing_key_rotation_requires_new_signature_without_resetting_replay_floor(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from hermes.config_sync.policy_document import PolicyBundle, signing_bytes
    from hermes.runtime.managed_llm import read_policy
    from hermes.runtime.model_config import ManagedProviderUnavailableError

    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)
    old = boot(path)
    signer = Ed25519PrivateKey.generate()
    store = wiring._association_store
    store.save(
        association=replace(
            store.get(), signing_pubkey_hex=signer.public_key().public_bytes_raw().hex()
        ),
        instance_secret="fictional-rotated-token",
    )
    assert reconcile_generation(path).mode == "blocked"
    with pytest.raises(ManagedProviderUnavailableError):
        read_policy(path)
    with pytest.raises(PermissionError):
        apply_signed_gateway(wiring, signed)
    bundle = PolicyBundle.model_validate_json(signed)
    canonical = signing_bytes(
        version=bundle.version,
        tenant_id=bundle.tenant_id,
        issued_at=bundle.issued_at,
        payload=bundle.payload,
    )
    bundle.signature_hex = signer.sign(canonical).hex()
    apply_signed_gateway(wiring, bundle.model_dump_json())
    assert boot(path).check().mode == "managed"
    assert read_policy(path)["version"] == 1
    with pytest.raises(LifecycleUnavailable):
        old.check()


def test_boot_cas_rejects_configuration_change_during_initialization(tmp_path):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    expected = reconcile_generation(path)
    apply_signed_gateway(wiring, signed)
    with pytest.raises(LifecycleUnavailable):
        acknowledge_boot(path, expected)
    assert reconcile_generation(path).restart_pending


def test_recovery_reconstructs_intent_after_old_writer_missing_lifecycle_record(tmp_path):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)
    old = boot(path)
    # Simulates an old binary/crash: source-of-truth changed without notification.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE instance_association SET state='revoked'")
    assert reconcile_generation(path).mode == "blocked"
    assert reconcile_generation(path).restart_pending
    with pytest.raises(LifecycleUnavailable):
        old.check()


def test_lifecycle_table_has_no_credentials_or_configuration_contents(tmp_path):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)
    boot(path)
    with sqlite3.connect(path) as conn:
        result = json.dumps(conn.execute("SELECT * FROM managed_llm_lifecycle").fetchall())
    assert "fictional" not in result and "https:" not in result and "company" not in result


def test_blocked_generation_never_admits_even_after_boot_ack(tmp_path):
    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)
    wiring._association_store.mark_revoked()
    with pytest.raises(LifecycleUnavailable):
        boot(path).check()


def test_persistence_failure_latches_process_even_after_recovery(tmp_path, monkeypatch):
    from hermes.runtime import managed_llm_lifecycle as module

    path = tmp_path / "state.db"
    old = boot(path)
    original = module.reconcile_generation
    monkeypatch.setattr(
        module,
        "reconcile_generation",
        lambda _: (_ for _ in ()).throw(LifecycleUnavailable("storage unavailable")),
    )
    with pytest.raises(LifecycleUnavailable):
        old.check()
    monkeypatch.setattr(module, "reconcile_generation", original)
    with pytest.raises(LifecycleUnavailable):
        old.check()


def test_config_lock_holder_can_close_latch_while_another_thread_checks(tmp_path):
    from hermes.security.configuration_lock import configuration_lock

    path = tmp_path / "state.db"
    guard = boot(path)
    started = threading.Event()
    outcome = []

    def check():
        started.set()
        try:
            guard.check()
            outcome.append("unexpected-admission")
        except LifecycleUnavailable:
            outcome.append("denied")

    with configuration_lock(path):
        thread = threading.Thread(target=check)
        thread.start()
        assert started.wait(timeout=1)
        time.sleep(0.05)
        before = time.monotonic()
        guard.close()
        assert time.monotonic() - before < 0.2
    thread.join(timeout=2)
    assert not thread.is_alive() and outcome == ["denied"]


@pytest.mark.asyncio
async def test_watcher_db_contention_does_not_block_event_loop_or_logical_close(tmp_path):
    from hermes.runtime.managed_llm_lifecycle import watch_authority
    from hermes.security.configuration_lock import configuration_lock

    path = tmp_path / "state.db"
    guard = boot(path)
    locked, release = threading.Event(), threading.Event()

    def hold():
        with configuration_lock(path):
            locked.set()
            release.wait(timeout=3)

    holder = threading.Thread(target=hold)
    holder.start()
    assert locked.wait(timeout=1)
    stopped = asyncio.Event()
    watcher = asyncio.create_task(watch_authority(guard, stopped.set, interval=0.01))
    try:
        before = time.monotonic()
        await asyncio.sleep(0.05)
        guard.close()
        assert time.monotonic() - before < 0.3
    finally:
        release.set()
        holder.join(timeout=2)
    await asyncio.wait_for(stopped.wait(), timeout=2)
    await watcher


@pytest.mark.asyncio
async def test_cancelled_await_keeps_running_native_worker_registered_until_shutdown(tmp_path):
    from hermes.runtime.managed_llm_lifecycle import run_admitted_native

    guard = boot(tmp_path / "state.db")
    started, interrupted, finished = threading.Event(), threading.Event(), threading.Event()

    def native():
        started.set()
        try:
            assert interrupted.wait(timeout=3)
        finally:
            finished.set()

    task = asyncio.create_task(run_admitted_native(guard, native, interrupted.set))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not finished.is_set()
        guard.close()
        assert guard._interrupts  # actual native thread is still running
        guard.interrupt_inflight()
        assert await asyncio.to_thread(finished.wait, 1)
        assert not guard._interrupts
    finally:
        interrupted.set()


@pytest.mark.asyncio
async def test_cancel_during_authority_wait_does_not_invoke_native_after_lock_release(tmp_path):
    from hermes.runtime.managed_llm_lifecycle import run_admitted_native
    from hermes.security.configuration_lock import configuration_lock

    path = tmp_path / "state.db"
    guard = boot(path)
    locked, release = threading.Event(), threading.Event()
    called = threading.Event()

    def hold():
        with configuration_lock(path):
            locked.set()
            release.wait(timeout=3)

    holder = threading.Thread(target=hold)
    holder.start()
    assert locked.wait(timeout=1)
    task = asyncio.create_task(run_admitted_native(guard, called.set, called.set))
    try:
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        holder.join(timeout=1)
    # Reconciliation synchronizes with the released worker; no callback is
    # registered before its authoritative check succeeds.
    await asyncio.to_thread(guard.check)
    await asyncio.sleep(0.05)
    assert not called.is_set() and not guard._interrupts


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["close", "revoke", "restore", "storage"])
async def test_native_result_after_authority_loss_is_cancelled_not_a_success(tmp_path, transition):
    from hermes.runtime.managed_llm_lifecycle import run_admitted_native
    from hermes.tasks.domain.task_cancel_registry import OperationCancelled

    path = tmp_path / "state.db"
    wiring, signed = _seed(path)
    apply_signed_gateway(wiring, signed)
    guard = boot(path)

    def native():
        if transition == "close":
            guard.close()
        elif transition == "storage":
            path.unlink()
            path.mkdir()
        else:
            original = wiring._association_store.get()
            wiring._association_store.mark_revoked()
            if transition == "restore":
                wiring._association_store.save(
                    association=original, instance_secret="fictional-restored-secret",
                )
                boot(path)
        # Native Hermes returns interruption/error as a dict, not necessarily
        # an exception. Even a success-looking result cannot renew authority.
        return {"final_response": "partial answer", "completed": True}

    with pytest.raises(OperationCancelled, match="autorización"):
        await run_admitted_native(guard, native, lambda: None)
    assert not guard._interrupts


@pytest.mark.asyncio
async def test_result_delivery_checks_latch_after_executor_releases_callback(tmp_path, monkeypatch):
    from hermes.runtime.managed_llm_lifecycle import run_admitted_native
    from hermes.tasks.domain.task_cancel_registry import OperationCancelled

    guard = boot(tmp_path / "state.db")
    original_register = guard.register_interrupt

    def register(callback):
        release = original_register(callback)

        def release_then_revoke():
            release()
            guard.close()

        return release_then_revoke

    monkeypatch.setattr(guard, "register_interrupt", register)
    with pytest.raises(OperationCancelled, match="autorización"):
        await run_admitted_native(guard, lambda: {"completed": True}, lambda: None)
    assert not guard._interrupts


@pytest.mark.asyncio
async def test_unchanged_native_result_and_error_preserve_their_original_contract(tmp_path):
    from hermes.runtime.managed_llm_lifecycle import run_admitted_native

    guard = boot(tmp_path / "state.db")
    result = {"completed": True, "final_response": "done"}
    assert await run_admitted_native(guard, lambda: result, lambda: None) is result
    assert await run_admitted_native(None, lambda: result, lambda: None) is result

    def fail():
        raise ValueError("synthetic provider failure")

    with pytest.raises(ValueError, match="synthetic provider failure"):
        await run_admitted_native(guard, fail, lambda: None)
    assert not guard._interrupts


@pytest.mark.asyncio
async def test_authority_loss_cancels_real_queue_without_retry_or_success_stream(tmp_path, caplog):
    from hermes.runtime.managed_llm_lifecycle import run_admitted_native
    from hermes.tasks.domain.ports import TaskStatus
    from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue
    from hermes.testing import FakeReasoningEngine, scripted_response
    from tests.tasks.test_agent_loop import _chat_item, _make_chat_orchestrator

    guard = boot(tmp_path / "state.db")
    queue_path = tmp_path / "queue.db"
    queue = SqliteWorkQueue(db_path=queue_path)

    class LosingAuthorityEngine(FakeReasoningEngine):
        async def run_cycle(self, context):
            def native():
                guard.close()
                return {"completed": True, "final_response": "must not be published"}

            await run_admitted_native(guard, native, lambda: None)
            return await super().run_cycle(context)

    engine = LosingAuthorityEngine(scripted=[scripted_response(narrative="must not be published")])
    orchestrator, _, sink = _make_chat_orchestrator(engine=engine, queue=queue)
    item = await queue.enqueue(_chat_item())
    claimed = await queue.claim_next()
    with caplog.at_level("INFO"):
        await orchestrator._process(claimed)
    restarted = SqliteWorkQueue(db_path=queue_path)
    assert (await restarted._load_item(str(item.id))).status is TaskStatus.CANCELLED
    assert await restarted.reconcile_stale() == 0
    assert await restarted.claim_next() is None
    assert [entry["outcome"] for entry in sink.closed] == ["cancelled"]
    assert sink.emitted == []
    assert "hermes.tasks.loop.task_completed" not in caplog.text
    assert "chat_replied" not in caplog.text
