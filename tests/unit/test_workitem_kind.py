"""T014 — campo `kind` en WorkItem.

Verifica:
- Default es 'autonomous' (retro-compatibilidad con P0).
- Construcción explícita con 'chat_message'.
- WorkItem.new() acepta kind y lo propaga.
- Las transiciones de estado heredadas de P0 son indiferentes al kind
  (claim/mark_completed/mark_failed/mark_rejected no mutan ni validan kind).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.tasks.domain.ports import TaskStatus, WorkItem, WorkItemKind
from hermes.tasks.domain.work_item import (
    claim,
    mark_completed,
    mark_failed,
    mark_rejected,
)

pytestmark = pytest.mark.unit

_TENANT = uuid4()


# ---------------------------------------------------------------------------
# Default y enum
# ---------------------------------------------------------------------------


def test_default_kind_is_autonomous() -> None:
    item = WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={},
    )
    assert item.kind is WorkItemKind.AUTONOMOUS


def test_kind_chat_message_explicit() -> None:
    item = WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="chat_message",
        payload={"user_message": "hello"},
        kind=WorkItemKind.CHAT_MESSAGE,
    )
    assert item.kind is WorkItemKind.CHAT_MESSAGE


def test_workitemkind_values() -> None:
    assert WorkItemKind.AUTONOMOUS == "autonomous"
    assert WorkItemKind.CHAT_MESSAGE == "chat_message"


# ---------------------------------------------------------------------------
# Transiciones heredadas de P0 son ciegas al kind
# ---------------------------------------------------------------------------


def _pending_autonomous() -> WorkItem:
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={"instruction": "do something"},
        kind=WorkItemKind.AUTONOMOUS,
    )


def _pending_chat() -> WorkItem:
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="chat_message",
        payload={"user_message": "hi"},
        kind=WorkItemKind.CHAT_MESSAGE,
    )


def test_claim_preserves_kind_autonomous() -> None:
    item = _pending_autonomous()
    claimed = claim(item, claim_token=uuid4())
    assert claimed.kind is WorkItemKind.AUTONOMOUS
    assert claimed.status is TaskStatus.IN_PROGRESS


def test_claim_preserves_kind_chat_message() -> None:
    item = _pending_chat()
    claimed = claim(item, claim_token=uuid4())
    assert claimed.kind is WorkItemKind.CHAT_MESSAGE
    assert claimed.status is TaskStatus.IN_PROGRESS


def test_mark_completed_preserves_kind() -> None:
    token = uuid4()
    item = claim(_pending_autonomous(), claim_token=token)
    completed = mark_completed(item, claim_token=token, audit_entry_id=uuid4())
    assert completed.kind is WorkItemKind.AUTONOMOUS
    assert completed.status is TaskStatus.COMPLETED


def test_mark_failed_terminal_preserves_kind() -> None:
    token = uuid4()
    # max_attempts=1 => terminal on first failure
    item = WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="chat_message",
        payload={},
        kind=WorkItemKind.CHAT_MESSAGE,
        max_attempts=1,
    )
    in_progress = claim(item, claim_token=token)
    failed = mark_failed(in_progress, claim_token=token, reason="no model")
    assert failed.kind is WorkItemKind.CHAT_MESSAGE
    assert failed.status is TaskStatus.FAILED


def test_mark_failed_retry_preserves_kind() -> None:
    token = uuid4()
    item = WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="chat_message",
        payload={},
        kind=WorkItemKind.CHAT_MESSAGE,
        max_attempts=3,
    )
    in_progress = claim(item, claim_token=token)
    retried = mark_failed(in_progress, claim_token=token, reason="transient")
    assert retried.kind is WorkItemKind.CHAT_MESSAGE
    assert retried.status is TaskStatus.PENDING


def test_mark_rejected_preserves_kind() -> None:
    token = uuid4()
    item = claim(_pending_chat(), claim_token=token)
    rejected = mark_rejected(item, claim_token=token, reason="policy")
    assert rejected.kind is WorkItemKind.CHAT_MESSAGE
    assert rejected.status is TaskStatus.REJECTED
