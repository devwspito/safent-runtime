"""SqlitePendingDelegationRepository — idempotencia + transición atómica."""

from __future__ import annotations

import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from hermes.tasks.infrastructure.sqlite_pending_delegations import (
    SqlitePendingDelegationRepository,
)


def _envelope(message_id: str = "msg-1") -> dict:
    return {
        "message_id": message_id,
        "correlation_id": "corr-1",
        "from_employee_id": "alice@org.example",
        "from_agent_id": "",
        "from_instance_id": "instance-B",
        "to_employee_id": "bob@org.example",
        "to_agent_id": "",
        "body": "please help with X",
        "issued_at": "2026-07-04T00:00:00+00:00",
    }


def test_submit_is_idempotent_insert_or_ignore():
    repo = SqlitePendingDelegationRepository.in_memory()

    status_1 = repo.submit(envelope=_envelope())
    status_2 = repo.submit(envelope=_envelope())

    assert status_1 == "pending"
    assert status_2 == "pending"
    assert len(repo.list_pending()) == 1


def test_resolve_only_transitions_from_pending():
    repo = SqlitePendingDelegationRepository.in_memory()
    repo.submit(envelope=_envelope())

    first = repo.resolve(message_id="msg-1", status="approved", resolved_by="admin-1")
    second = repo.resolve(message_id="msg-1", status="rejected", resolved_by="admin-2")

    assert first is True
    assert second is False  # already resolved — no-op, never re-resolved
    row = repo.fetch(message_id="msg-1")
    assert row is not None
    assert row.status == "approved"
    assert row.resolved_by == "admin-1"


def test_resolved_rows_are_not_listed_as_pending():
    repo = SqlitePendingDelegationRepository.in_memory()
    repo.submit(envelope=_envelope())
    repo.resolve(message_id="msg-1", status="rejected", resolved_by="admin-1")

    assert repo.list_pending() == []


def test_fetch_unknown_message_id_returns_none():
    repo = SqlitePendingDelegationRepository.in_memory()
    assert repo.fetch(message_id="does-not-exist") is None


def test_resolve_unknown_message_id_returns_false():
    repo = SqlitePendingDelegationRepository.in_memory()
    resolved = repo.resolve(message_id="does-not-exist", status="approved", resolved_by="admin-1")
    assert resolved is False


def _race_decision(path: str, decision: str, barrier, results) -> None:
    repo = SqlitePendingDelegationRepository(Path(path))
    barrier.wait(timeout=10)
    if decision == "approve":
        result = repo.claim_approval(
            message_id="msg-1", approved_by="owner", conversation_id="conv"
        )
    else:
        result = repo.resolve(message_id="msg-1", status="rejected", resolved_by="owner")
    results.put((decision, bool(result)))
    repo._conn.close()


@pytest.mark.parametrize("opponent", ["approve", "reject"])
def test_admission_claim_is_single_winner_across_processes(tmp_path, opponent):
    path = tmp_path / "claims.db"
    repo = SqlitePendingDelegationRepository(path)
    repo.submit(envelope=_envelope())
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    workers = [
        ctx.Process(target=_race_decision, args=(str(path), decision, barrier, results))
        for decision in ("approve", opponent)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert sum(results.get(timeout=2)[1] for _ in workers) == 1


def test_uncertain_claim_survives_restart_and_cannot_be_rejected_or_reapproved(tmp_path):
    path = tmp_path / "claims.db"
    repo = SqlitePendingDelegationRepository(path)
    repo.submit(envelope=_envelope())
    claim = repo.claim_approval(message_id="msg-1", approved_by="owner", conversation_id="conv")
    repo._conn.close()
    restarted = SqlitePendingDelegationRepository(path)
    assert restarted.list_pending()[0].admission_state == "unconfirmed"
    assert (
        restarted.claim_approval(message_id="msg-1", approved_by="other", conversation_id="other")
        is None
    )
    assert not restarted.resolve(message_id="msg-1", status="rejected", resolved_by="owner")
    assert not restarted.resolve(
        message_id="msg-1",
        status="approved",
        resolved_by="owner",
        claim_id="wrong",
        conversation_id="conv",
        task_id="task",
    )
    assert restarted.resolve(
        message_id="msg-1",
        status="approved",
        resolved_by="owner",
        claim_id=claim,
        conversation_id="conv",
        task_id="task",
    )


def test_pending_read_failure_is_not_an_empty_inbox():
    repo = SqlitePendingDelegationRepository.in_memory()
    repo._conn.close()
    with pytest.raises(sqlite3.Error):
        repo.list_pending()
