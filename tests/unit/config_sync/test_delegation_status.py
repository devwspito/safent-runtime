"""Durable telemetry is evidence, never permission or disclosure of chat."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from hermes.config_sync import delegation_status as ds
from hermes.tasks.infrastructure.schema import ensure_tasks_schema
from hermes.tasks.infrastructure.sqlite_pending_delegations import SqlitePendingDelegationRepository

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "shell-state.db"
    conn = sqlite3.connect(path)
    ensure_tasks_schema(conn)
    repo = SqlitePendingDelegationRepository(path)
    yield path, conn, repo
    repo._conn.close()
    conn.close()


def submit(db, message="request-1", recipient="instance-1"):
    db[2].submit(
        envelope={
            "message_id": message,
            "correlation_id": message,
            "from_employee_id": "owner",
            "from_instance_id": "console:org:owner",
            "to_instance_id": recipient,
            "body": "SECRET instruction never sent",
            "issued_at": "2026-09-11T10:00:00Z",
        }
    )


def task(db, status="pending", task_id="task-1"):
    db[1].execute(
        """INSERT INTO agent_tasks (
        task_id,trigger_kind,enqueued_by,operator_id,instruction,status,
        created_at,updated_at,execution_audit_entry_id,execution_head_hash,
        claim_token,claimed_at,lease_expires_at,worker_id
    ) VALUES (?,'manual_enqueue','owner','owner','PRIVATE chat',?,
        '2026-09-11','2026-09-11','audit','hash',?,'2026-09-11',?,'worker')""",
        (
            task_id,
            status,
            "claim" if status == "in_progress" else None,
            "2026-09-12" if status == "in_progress" else None,
        ),
    )
    db[1].commit()
    db[2].resolve(message_id="request-1", status="approved", resolved_by="owner", task_id=task_id)


def events(db):
    return [
        json.loads(r[0])
        for r in db[1].execute(
            "SELECT payload FROM delegation_status_outbox ORDER BY request_id,sequence"
        )
    ]


def collect(db, instance="instance-1"):
    return ds.collect_status_events(db[0], instance_id=instance)


def flush(db, current=lambda: True):
    return ds.push_status_events(
        db[0],
        instance_id="instance-1",
        cloud_endpoint="https://enterprise.example",
        instance_secret="SECRET bearer",
        is_current=current,
    )


def receipt(_url, **kwargs):
    event = kwargs["json"]
    return httpx.Response(
        200,
        json={
            "accepted": True,
            "ignored": False,
            "event_id": event["event_id"],
            "sequence": event["sequence"],
        },
    )


def test_pending_is_not_execution_and_snapshots_survive_reopen(db):
    submit(db)
    assert collect(db) == 1
    assert collect(db) == 0
    first = events(db)[0]
    assert first["status"] == "awaiting_approval"
    assert first["task_id"] is None
    task(db)
    assert collect(db) == 1
    assert events(db)[1]["status"] == "queued"
    assert events(db)[1]["sequence"] == 2
    assert events(db)[0] == first
    assert all(set(e) == {"event_id", "sequence", "status", "task_id"} for e in events(db))
    assert "SECRET" not in json.dumps(events(db))
    assert "PRIVATE" not in json.dumps(events(db))


@pytest.mark.parametrize(
    "local,remote",
    [
        ("pending", "queued"),
        ("in_progress", "running"),
        ("pending_approval", "blocked"),
        ("completed", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("rejected", "rejected"),
    ],
)
def test_only_observed_execution_states(db, local, remote):
    submit(db)
    task(db, local)
    assert collect(db) == 1
    assert events(db)[0]["status"] == remote
    assert len(events(db)) == 1  # No fabricated intermediate transitions.


def test_unlinked_approved_task_is_unknown(db):
    submit(db)
    db[2].resolve(message_id="request-1", status="approved", resolved_by="owner", task_id="missing")
    assert collect(db) == 0


def test_rejected_request_requires_no_task(db):
    submit(db)
    db[2].resolve(message_id="request-1", status="rejected", resolved_by="owner")
    collect(db)
    assert events(db)[0]["status"] == "rejected"


def test_legacy_unknown_and_other_recipient_never_emit(db):
    submit(db, "legacy", None)
    submit(db, "other", "instance-2")
    assert collect(db) == 0
    assert collect(db, "instance-2") == 1
    assert db[1].execute("SELECT request_id FROM delegation_status_outbox").fetchall() == [
        ("other",)
    ]


def test_terminal_regression_and_task_rebinding_fail_closed(db):
    submit(db)
    task(db, "completed")
    collect(db)
    original = events(db)
    db[1].execute("UPDATE agent_tasks SET status='pending'")
    db[1].commit()
    with pytest.raises(ValueError, match="terminal"):
        collect(db)
    assert events(db) == original
    db[1].execute("UPDATE agent_tasks SET status='completed'")
    db[1].execute("UPDATE pending_delegations SET task_id='task-2'")
    db[1].commit()
    task(db, "completed", "task-2")
    with pytest.raises(ValueError, match="identity"):
        collect(db)
    assert events(db) == original


def test_concurrent_collectors_persist_one_sequence(db):
    submit(db)
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(lambda _: collect(db), range(8)))
    assert sum(counts) == 1
    assert len(events(db)) == 1


def test_failure_then_retry_reuses_event_identity(db, monkeypatch):
    submit(db)
    post = Mock(side_effect=httpx.ConnectError("SECRET provider message"))
    monkeypatch.setattr(ds.httpx, "post", post)
    assert flush(db) == 0
    event = post.call_args.kwargs["json"]
    post.side_effect = receipt
    assert flush(db) == 1
    assert post.call_args.kwargs["json"] == event
    assert post.call_args.kwargs["follow_redirects"] is False
    assert flush(db) == 0
    assert post.call_count == 2


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"accepted": "true"},
        {"accepted": True, "sequence": True},
        {"accepted": True, "event_id": "wrong"},
        {"accepted": True, "ignored": "false"},
    ],
)
def test_bad_receipt_never_marks_delivered(db, monkeypatch, bad):
    submit(db)
    monkeypatch.setattr(ds.httpx, "post", Mock(return_value=httpx.Response(200, json=bad)))
    assert flush(db) == 0
    assert db[1].execute("SELECT delivered FROM delegation_status_outbox").fetchone() == (0,)


@pytest.mark.parametrize("code", [301, 401, 403, 404, 409, 429, 500])
def test_http_failures_retain_outbox(db, monkeypatch, code):
    submit(db)
    monkeypatch.setattr(ds.httpx, "post", Mock(return_value=httpx.Response(code)))
    assert flush(db) == 0
    assert len(events(db)) == 1


def test_pairing_revocation_stops_remaining_events(db, monkeypatch):
    submit(db)
    submit(db, "request-2")
    post = Mock(side_effect=receipt)
    monkeypatch.setattr(ds.httpx, "post", post)
    current = Mock(side_effect=[True, False])
    assert flush(db, current) == 1
    assert post.call_count == 1
    assert flush(db, lambda: False) == 0
    assert post.call_count == 1


def test_bounded_flush_and_encoded_request_id(db, monkeypatch):
    for index in range(12):
        submit(db, f"{index:02d}/request")
    post = Mock(side_effect=receipt)
    monkeypatch.setattr(ds.httpx, "post", post)
    assert flush(db) == 8
    assert post.call_count == 8
    assert post.call_args_list[0].args[0].endswith("/00%2Frequest/status")
    assert flush(db) == 4


def test_legacy_schema_migration_preserves_unknown_recipient(tmp_path):
    path = tmp_path / "legacy.db"
    repo = SqlitePendingDelegationRepository(path)
    repo._conn.execute("ALTER TABLE pending_delegations DROP COLUMN to_instance_id")
    repo._conn.commit()
    repo._conn.close()
    reopened = SqlitePendingDelegationRepository(path)
    try:
        assert "to_instance_id" in {
            r[1] for r in reopened._conn.execute("PRAGMA table_info(pending_delegations)")
        }
    finally:
        reopened._conn.close()


@pytest.mark.parametrize(
    "changed",
    [
        {"state": "revoked"},
        {"instance_id": "new-instance"},
        {"tenant_id": "other"},
        {"paired_at": "new-pairing"},
        {"cloud_endpoint": "https://elsewhere.example"},
        {"signing_pubkey_hex": "22" * 32},
    ],
)
async def test_orchestration_rechecks_pairing_in_worker_thread(db, monkeypatch, changed):
    from hermes.config_sync import delegation_inbox as di

    assoc = SimpleNamespace(
        state="active",
        instance_id="instance-1",
        tenant_id="org",
        paired_at="original",
        cloud_endpoint="https://enterprise.example",
        signing_pubkey_hex="11" * 32,
    )
    altered = SimpleNamespace(**{**vars(assoc), **changed})
    store = Mock()
    store.get.side_effect = [assoc, altered]
    store.is_associated.return_value = True
    store.reveal_instance_secret.return_value = "SECRET"
    checks = []

    def flush_checked(*_args, **kwargs):
        checks.append(kwargs["is_current"]())
        return 0

    monkeypatch.setattr(ds, "push_status_events", flush_checked)
    monkeypatch.setattr(di, "push_pending_delegation_results_once", Mock())
    monkeypatch.setattr(di, "poll_and_apply_inbox_once", AsyncMock())
    await di.run_delegation_inbox_once(store=store, proxy=Mock(), db_path=db[0])
    assert checks == [False]


async def test_telemetry_failure_cannot_block_inbox_or_log_secrets(db, monkeypatch, caplog):
    from hermes.config_sync import delegation_inbox as di

    assoc = SimpleNamespace(
        state="active",
        instance_id="instance-1",
        tenant_id="org",
        paired_at="original",
        cloud_endpoint="https://enterprise.example",
        signing_pubkey_hex="11" * 32,
    )
    store = Mock()
    store.get.return_value = assoc
    store.is_associated.return_value = True
    store.reveal_instance_secret.return_value = "SECRET"
    monkeypatch.setattr(
        ds, "push_status_events", Mock(side_effect=ValueError("SECRET private error"))
    )
    results, poll = Mock(), AsyncMock()
    monkeypatch.setattr(di, "push_pending_delegation_results_once", results)
    monkeypatch.setattr(di, "poll_and_apply_inbox_once", poll)
    await di.run_delegation_inbox_once(store=store, proxy=Mock(), db_path=db[0])
    results.assert_called_once()
    poll.assert_awaited_once()
    assert "delegation_status.unavailable" in caplog.text
    assert "SECRET" not in caplog.text
