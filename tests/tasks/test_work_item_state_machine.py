"""Tests máquina de estados WorkItem / TaskStatus — deben FALLAR antes de T012.

Cubre:
- Transiciones legales según data-model.md
- Rechazo de transiciones ilegales
- I4: contadores de reintento coherentes
- I6: un solo estado a la vez (status único)
- I3: IN_PROGRESS exige claim_token + lease
- I2: terminal => sin claim/lease vivos
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from hermes.tasks.domain.ports import TaskStatus, WorkItem
from hermes.tasks.domain.work_item import (
    IllegalTransition,
    claim,
    mark_completed,
    mark_failed,
    mark_pending_approval,
    mark_rejected,
    to_pending_after_approval,
)

pytestmark = pytest.mark.unit

_TENANT = uuid4()
_CLAIM = uuid4()
_AUDIT = uuid4()
_PROPOSAL = uuid4()


def _pending() -> WorkItem:
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={"instruction": "test"},
    )


def _in_progress(attempts: int = 1, max_attempts: int = 3) -> WorkItem:
    base = _pending()
    now = datetime.now(tz=UTC)
    return WorkItem(
        id=base.id,
        tenant_id=base.tenant_id,
        trigger_kind=base.trigger_kind,
        payload=base.payload,
        status=TaskStatus.IN_PROGRESS,
        attempts=attempts,
        max_attempts=max_attempts,
        claim_token=_CLAIM,
        claimed_at=now,
        lease_expires_at=now + timedelta(seconds=60),
        enqueued_at=base.enqueued_at,
    )


class TestClaimTransition:
    def test_pending_to_in_progress(self) -> None:
        item = _pending()
        claimed = claim(item, claim_token=_CLAIM)
        assert claimed.status is TaskStatus.IN_PROGRESS

    def test_claim_sets_claim_token(self) -> None:
        item = _pending()
        claimed = claim(item, claim_token=_CLAIM)
        assert claimed.claim_token == _CLAIM

    def test_claim_sets_claimed_at(self) -> None:
        item = _pending()
        claimed = claim(item, claim_token=_CLAIM)
        assert claimed.claimed_at is not None

    def test_claim_sets_lease(self) -> None:
        item = _pending()
        claimed = claim(item, claim_token=_CLAIM)
        assert claimed.lease_expires_at is not None
        assert claimed.lease_expires_at > datetime.now(tz=UTC)

    def test_claim_increments_attempts(self) -> None:
        item = _pending()
        claimed = claim(item, claim_token=_CLAIM)
        assert claimed.attempts == 1

    def test_cannot_claim_in_progress(self) -> None:
        item = _in_progress()
        with pytest.raises(IllegalTransition):
            claim(item, claim_token=uuid4())

    def test_cannot_claim_completed(self) -> None:
        item = _pending()
        item = WorkItem(
            id=item.id,
            tenant_id=item.tenant_id,
            trigger_kind=item.trigger_kind,
            payload=item.payload,
            status=TaskStatus.COMPLETED,
            attempts=1,
            max_attempts=3,
            enqueued_at=item.enqueued_at,
        )
        with pytest.raises(IllegalTransition):
            claim(item, claim_token=uuid4())

    def test_cannot_claim_rejected(self) -> None:
        item = WorkItem(
            id=uuid4(),
            tenant_id=_TENANT,
            trigger_kind="manual_enqueue",
            payload={},
            status=TaskStatus.REJECTED,
            enqueued_at=datetime.now(tz=UTC),
        )
        with pytest.raises(IllegalTransition):
            claim(item, claim_token=uuid4())


class TestCompletedTransition:
    def test_in_progress_to_completed(self) -> None:
        item = _in_progress()
        done = mark_completed(item, claim_token=_CLAIM, audit_entry_id=_AUDIT)
        assert done.status is TaskStatus.COMPLETED

    def test_completed_clears_claim_and_lease(self) -> None:
        item = _in_progress()
        done = mark_completed(item, claim_token=_CLAIM, audit_entry_id=_AUDIT)
        # I2: terminal => sin claim/lease
        assert done.claim_token is None
        assert done.lease_expires_at is None

    def test_completed_wrong_claim_token_raises(self) -> None:
        item = _in_progress()
        with pytest.raises(IllegalTransition, match="claim_token"):
            mark_completed(item, claim_token=uuid4(), audit_entry_id=_AUDIT)

    def test_cannot_complete_from_pending(self) -> None:
        item = _pending()
        with pytest.raises(IllegalTransition):
            mark_completed(item, claim_token=_CLAIM, audit_entry_id=_AUDIT)


class TestFailedTransition:
    def test_in_progress_to_failed_terminal(self) -> None:
        item = _in_progress(attempts=3, max_attempts=3)
        failed = mark_failed(item, claim_token=_CLAIM, reason="oops")
        assert failed.status is TaskStatus.FAILED
        assert failed.attempts == 3

    def test_failed_retryable_resets_to_pending(self) -> None:
        item = _in_progress(attempts=1, max_attempts=3)
        failed = mark_failed(item, claim_token=_CLAIM, reason="transient")
        # Re-scheduled as PENDING with backoff
        assert failed.status is TaskStatus.PENDING

    def test_failed_retryable_has_future_available_at(self) -> None:
        item = _in_progress(attempts=1, max_attempts=3)
        failed = mark_failed(item, claim_token=_CLAIM, reason="transient")
        assert failed.available_at > datetime.now(tz=UTC)

    def test_failed_clears_claim_and_lease(self) -> None:
        item = _in_progress(attempts=3, max_attempts=3)
        failed = mark_failed(item, claim_token=_CLAIM, reason="out of retries")
        # I2: terminal => no claim/lease
        assert failed.claim_token is None
        assert failed.lease_expires_at is None

    def test_failed_wrong_claim_token_raises(self) -> None:
        item = _in_progress()
        with pytest.raises(IllegalTransition, match="claim_token"):
            mark_failed(item, claim_token=uuid4(), reason="bad token")

    def test_failed_cannot_exceed_max_attempts(self) -> None:
        item = _in_progress(attempts=3, max_attempts=3)
        failed = mark_failed(item, claim_token=_CLAIM, reason="exhausted")
        # I4: attempts <= max_attempts
        assert failed.attempts <= failed.max_attempts


class TestPendingApprovalTransition:
    def test_in_progress_to_pending_approval(self) -> None:
        item = _in_progress()
        pa = mark_pending_approval(item, claim_token=_CLAIM, proposal_id=_PROPOSAL)
        assert pa.status is TaskStatus.PENDING_APPROVAL

    def test_pending_approval_clears_lease(self) -> None:
        item = _in_progress()
        pa = mark_pending_approval(item, claim_token=_CLAIM, proposal_id=_PROPOSAL)
        # Libera lease para no bloquear cola (FR-024)
        assert pa.lease_expires_at is None
        assert pa.claim_token is None

    def test_pending_approval_wrong_claim_raises(self) -> None:
        item = _in_progress()
        with pytest.raises(IllegalTransition, match="claim_token"):
            mark_pending_approval(item, claim_token=uuid4(), proposal_id=_PROPOSAL)

    def test_to_pending_after_approval(self) -> None:
        item = _in_progress()
        pa = mark_pending_approval(item, claim_token=_CLAIM, proposal_id=_PROPOSAL)
        re_enqueued = to_pending_after_approval(pa)
        assert re_enqueued.status is TaskStatus.PENDING
        assert re_enqueued.available_at <= datetime.now(tz=UTC) + timedelta(seconds=1)


class TestRejectedTransition:
    def test_in_progress_to_rejected(self) -> None:
        item = _in_progress()
        rejected = mark_rejected(item, claim_token=_CLAIM, reason="policy")
        assert rejected.status is TaskStatus.REJECTED

    def test_rejected_clears_claim_and_lease(self) -> None:
        item = _in_progress()
        rejected = mark_rejected(item, claim_token=_CLAIM, reason="policy")
        assert rejected.claim_token is None
        assert rejected.lease_expires_at is None

    def test_rejected_wrong_claim_raises(self) -> None:
        item = _in_progress()
        with pytest.raises(IllegalTransition, match="claim_token"):
            mark_rejected(item, claim_token=uuid4(), reason="policy")


class TestInvariantI6:
    """I6: una fila, un solo estado (status es único — no flags paralelos)."""

    def test_work_item_has_single_status(self) -> None:
        item = _pending()
        # There is exactly one status field, and it equals exactly one value
        assert item.status in TaskStatus
        statuses = [s for s in TaskStatus if item.status == s]
        assert len(statuses) == 1

    def test_transition_produces_new_instance(self) -> None:
        item = _pending()
        claimed = claim(item, claim_token=_CLAIM)
        # Immutable — transition produces a new instance
        assert item is not claimed
        assert item.status is TaskStatus.PENDING
        assert claimed.status is TaskStatus.IN_PROGRESS


class TestInvariantI4:
    """I4: contadores de reintento coherentes (attempts>=0, attempts<=max_attempts)."""

    def test_attempts_starts_at_zero(self) -> None:
        item = _pending()
        assert item.attempts == 0

    def test_attempts_increases_on_claim(self) -> None:
        item = _pending()
        claimed = claim(item, claim_token=_CLAIM)
        assert claimed.attempts == 1

    def test_attempts_never_exceeds_max(self) -> None:
        item = _in_progress(attempts=3, max_attempts=3)
        failed = mark_failed(item, claim_token=_CLAIM, reason="exhausted")
        assert failed.attempts <= failed.max_attempts

    def test_max_attempts_must_be_positive(self) -> None:
        # WorkItem.new defaults to max_attempts=3; invariant is maintained by transitions
        item = _pending()
        assert item.max_attempts > 0
