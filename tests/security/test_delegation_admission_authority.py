"""Real signed proof + SQLite pairing: no retroactive trust or stale admission."""

import multiprocessing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes.config_sync.delegation_inbox import delegation_signing_bytes
from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore
from hermes.shell_server.security.secrets import SecretsVault
from hermes.tasks.infrastructure.sqlite_pending_delegations import SqlitePendingDelegationRepository
from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
from hermes.tasks.triggers.application.delegation_approval_service import DelegationApprovalService
from hermes.tasks.triggers.application.delegation_authority import (
    DelegationAdmissionAuthority,
    DelegationAuthorityError,
)
from hermes.tasks.triggers.application.trigger_gate import TriggerGate
from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (
    SqliteAuthorizedTriggerRepository,
)


@pytest.fixture
def setup(tmp_path):
    private = Ed25519PrivateKey.generate()
    store = SQLiteAssociationStore(
        db_path=tmp_path / "pairing.db", vault=SecretsVault(master_key=b"k" * 32)
    )
    assoc = InstanceAssociation(
        instance_id=str(uuid4()),
        tenant_id=str(uuid4()),
        paired_at=datetime.now(UTC).isoformat(),
        cloud_endpoint="https://fixture.invalid",
        signing_pubkey_hex=private.public_key().public_bytes_raw().hex(),
        license={},
        last_applied_version=0,
        state="active",
    )
    store.save(association=assoc, instance_secret="fixture-only")
    pending = SqlitePendingDelegationRepository(tmp_path / "tasks.db")
    authority = DelegationAdmissionAuthority(association_store=store, pending_repo=pending)
    queue = SqliteWorkQueue(db_path=tmp_path / "tasks.db")
    triggers = SqliteAuthorizedTriggerRepository.in_memory()

    class Conversations:
        def create_or_touch(self, **kwargs):
            pass

        def append_message(self, **kwargs):
            pass

    service = DelegationApprovalService(
        pending_repo=pending,
        trigger_repo=triggers,
        conversation_repo=Conversations(),
        authority=authority,
        gate=TriggerGate(
            trigger_repo=triggers,
            queue=queue,
            agent_state=InMemoryAgentState(),
            tenant_id=uuid4(),
        ),
    )
    envelope = dict(
        message_id=str(uuid4()),
        correlation_id=str(uuid4()),
        from_employee_id="remote-human",
        from_agent_id="",
        from_instance_id=str(uuid4()),
        to_employee_id="local-human",
        to_agent_id="",
        to_instance_id=assoc.instance_id,
        body="Narrative fixture only",
        kind="request",
        nonce=str(uuid4()),
        issued_at=datetime.now(UTC).isoformat(),
    )

    def sign(value):
        plain = {k: v for k, v in value.items() if k != "signature_hex"}
        return {**plain, "signature_hex": private.sign(delegation_signing_bytes(plain)).hex()}

    return service, pending, authority, store, assoc, queue, triggers, sign(envelope), sign


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("instance_id", "new-instance"),
        ("tenant_id", "new-tenant"),
        ("paired_at", "2026-09-12T10:00:00+00:00"),
        ("cloud_endpoint", "https://another.invalid"),
        ("signing_pubkey_hex", "00" * 32),
        ("state", "revoked"),
    ],
)
async def test_pairing_change_never_admits_or_rebinds_old_request(setup, field, value):
    service, pending, authority, store, assoc, queue, _, envelope, _sign = setup
    await service.submit(envelope=envelope)
    proof = pending.admission_proof(message_id=envelope["message_id"])
    store.save(association=replace(assoc, **{field: value}), instance_secret="fixture-only")
    assert await service.approve(message_id=envelope["message_id"], approved_by=uuid4()) is None
    assert await queue.claim_next() is None
    assert service.list_pending()[0]["admission_state"] == "unverified"
    assert pending.admission_proof(message_id=envelope["message_id"]) == proof
    assert await service.reject(message_id=envelope["message_id"], rejected_by=uuid4())


@pytest.mark.asyncio
async def test_legacy_redelivery_does_not_manufacture_proof(setup):
    service, pending, _, _, _, queue, _, envelope, _ = setup
    pending.submit(envelope=envelope)
    await service.submit(envelope=envelope)
    assert pending.admission_proof(message_id=envelope["message_id"]) is None
    assert await service.approve(message_id=envelope["message_id"], approved_by=uuid4()) is None
    assert await queue.claim_next() is None
    assert pending.fetch(message_id=envelope["message_id"]).body == envelope["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "repaired", "expired"])
async def test_change_during_authorization_await_is_checked_at_queue_commit(
    setup, change, monkeypatch
):
    service, pending, _, store, assoc, queue, triggers, envelope, _ = setup
    await service.submit(envelope=envelope)
    original = triggers.authorize

    async def changing_authorize(**kwargs):
        result = await original(**kwargs)
        if change == "revoked":
            store.mark_revoked()
        elif change == "repaired":
            store.save(
                association=replace(assoc, paired_at="new-binding"), instance_secret="fixture-only"
            )
        else:
            monkeypatch.setattr(
                "hermes.tasks.triggers.application.delegation_authority._freshness_ok",
                lambda _value: False,
            )
        return result

    monkeypatch.setattr(triggers, "authorize", changing_authorize)
    assert await service.approve(message_id=envelope["message_id"], approved_by=uuid4()) is None
    assert await queue.claim_next() is None
    assert service.list_pending()[0]["admission_state"] == "unverified"
    assert (
        pending._conn.execute("SELECT count(*) FROM delegation_admission_claims").fetchone()[0] == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["destination", "kind", "expired", "future", "signature", "extra", "body"]
)
async def test_daemon_authority_rejects_invalid_signed_shape_or_freshness(setup, mutation):
    service, pending, _, _, _, queue, _, envelope, sign = setup
    if mutation == "destination":
        envelope["to_instance_id"] = "elsewhere"
    elif mutation == "kind":
        envelope["kind"] = "result"
    elif mutation in {"expired", "future"}:
        envelope["issued_at"] = (
            datetime.now(UTC) + timedelta(days=-2 if mutation == "expired" else 2)
        ).isoformat()
    elif mutation == "extra":
        envelope["unexpected"] = "signed-but-unrecognized"
    envelope = sign(envelope)
    if mutation == "signature":
        envelope["signature_hex"] = "00" * 64
    if mutation == "body":
        envelope["body"] = "tampered"
    with pytest.raises(DelegationAuthorityError):
        await service.submit(envelope=envelope)
    assert pending.list_pending() == [] and await queue.claim_next() is None


@pytest.mark.asyncio
async def test_saved_row_cannot_drift_from_its_signed_proof(setup):
    service, pending, _, _, _, queue, _, envelope, _ = setup
    await service.submit(envelope=envelope)
    pending._conn.execute("UPDATE pending_delegations SET body='different action'")
    pending._conn.commit()
    assert await service.approve(message_id=envelope["message_id"], approved_by=uuid4()) is None
    assert await queue.claim_next() is None


@pytest.mark.asyncio
async def test_current_proof_survives_restart_and_queues_exactly_once(setup):
    service, pending, authority, store, assoc, queue, _, envelope, _ = setup
    await service.submit(envelope=envelope)
    reopened = SqlitePendingDelegationRepository(pending._db_path)
    DelegationAdmissionAuthority(association_store=store, pending_repo=reopened).validate(
        envelope["message_id"]
    )
    # A new policy version alone is not a new pairing.
    store.save(association=replace(assoc, last_applied_version=10), instance_secret="fixture-only")
    task_id = await service.approve(message_id=envelope["message_id"], approved_by=uuid4())
    assert task_id and (await queue.claim_next()).id == task_id
    assert await service.approve(message_id=envelope["message_id"], approved_by=uuid4()) is None


def _revoke_in_other_process(db_path, ready, start, attempted, finished):
    store = SQLiteAssociationStore(db_path=Path(db_path), vault=SecretsVault(master_key=b"k" * 32))
    ready.set()
    assert start.wait(10)
    attempted.set()
    store.mark_revoked()
    finished.set()


@pytest.mark.asyncio
async def test_final_commit_is_serialized_against_cross_process_revocation(setup, monkeypatch):
    service, _, _, store, _, queue, _, envelope, _ = setup
    await service.submit(envelope=envelope)
    ctx = multiprocessing.get_context("spawn")
    ready, start, attempted, finished = (ctx.Event() for _ in range(4))
    worker = ctx.Process(
        target=_revoke_in_other_process,
        args=(str(store.db_path), ready, start, attempted, finished),
    )
    worker.start()
    assert ready.wait(10)
    original = queue._enqueue

    def racing_insert(item):
        start.set()
        assert attempted.wait(10)
        # A process attempting revoke cannot commit inside our guarded insert.
        assert not finished.wait(0.15)
        assert store.get().state == "active"
        return original(item)

    monkeypatch.setattr(queue, "_enqueue", racing_insert)
    try:
        task_id = await service.approve(message_id=envelope["message_id"], approved_by=uuid4())
        assert task_id
        assert finished.wait(10)
        assert store.get().state == "revoked"
        assert (await queue.claim_next()).id == task_id
    finally:
        start.set()
        worker.join(10)
        if worker.is_alive():
            worker.terminate()
            worker.join()
    assert worker.exitcode == 0
