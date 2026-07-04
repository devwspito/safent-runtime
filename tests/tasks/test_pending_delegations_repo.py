"""SqlitePendingDelegationRepository — idempotencia + transición atómica."""

from __future__ import annotations

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

    first = repo.resolve(
        message_id="msg-1", status="approved", resolved_by="admin-1"
    )
    second = repo.resolve(
        message_id="msg-1", status="rejected", resolved_by="admin-2"
    )

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
    resolved = repo.resolve(
        message_id="does-not-exist", status="approved", resolved_by="admin-1"
    )
    assert resolved is False
