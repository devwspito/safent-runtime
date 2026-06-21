"""Tests for InputOwnershipLedger — poseedor único invariant (FR-002/FR-022)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.agents_os.application.teaching.input_ownership_ledger import (
    InputOwnershipLedger,
)
from hermes.agents_os.application.teaching.teaching_context import (
    InputOwner,
    InputOwnershipViolation,
)

pytestmark = pytest.mark.unit


class TestClaimFreshContext:
    def test_first_claim_succeeds(self) -> None:
        ledger = InputOwnershipLedger()
        ctx_id = uuid4()
        ledger.claim(ctx_id, InputOwner.OPERATOR)
        assert ledger.owner_of(ctx_id) == InputOwner.OPERATOR

    def test_claim_agent_succeeds(self) -> None:
        ledger = InputOwnershipLedger()
        ctx_id = uuid4()
        ledger.claim(ctx_id, InputOwner.AGENT)
        assert ledger.owner_of(ctx_id) == InputOwner.AGENT


class TestClaimIdempotency:
    def test_same_owner_claim_twice_is_noop(self) -> None:
        """Idempotent claim by same owner must not raise (retry safety)."""
        ledger = InputOwnershipLedger()
        ctx_id = uuid4()
        ledger.claim(ctx_id, InputOwner.OPERATOR)
        ledger.claim(ctx_id, InputOwner.OPERATOR)  # must not raise
        assert ledger.owner_of(ctx_id) == InputOwner.OPERATOR


class TestDoubleClaim:
    def test_different_owner_raises_violation(self) -> None:
        """FR-002 fail-closed: second owner on claimed context → violation."""
        ledger = InputOwnershipLedger()
        ctx_id = uuid4()
        ledger.claim(ctx_id, InputOwner.OPERATOR)
        with pytest.raises(InputOwnershipViolation):
            ledger.claim(ctx_id, InputOwner.AGENT)

    def test_agent_then_operator_raises(self) -> None:
        ledger = InputOwnershipLedger()
        ctx_id = uuid4()
        ledger.claim(ctx_id, InputOwner.AGENT)
        with pytest.raises(InputOwnershipViolation):
            ledger.claim(ctx_id, InputOwner.OPERATOR)


class TestRelease:
    def test_release_frees_context(self) -> None:
        ledger = InputOwnershipLedger()
        ctx_id = uuid4()
        ledger.claim(ctx_id, InputOwner.OPERATOR)
        ledger.release(ctx_id)
        assert ledger.owner_of(ctx_id) is None

    def test_release_unknown_is_noop(self) -> None:
        ledger = InputOwnershipLedger()
        ledger.release(uuid4())  # must not raise

    def test_re_claim_after_release_succeeds(self) -> None:
        ledger = InputOwnershipLedger()
        ctx_id = uuid4()
        ledger.claim(ctx_id, InputOwner.OPERATOR)
        ledger.release(ctx_id)
        ledger.claim(ctx_id, InputOwner.AGENT)
        assert ledger.owner_of(ctx_id) == InputOwner.AGENT
