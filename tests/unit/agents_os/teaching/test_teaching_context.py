"""Tests for TeachingContext VO invariants (FR-003/FR-018)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.agents_os.application.teaching.teaching_context import (
    InputOwner,
    InputOwnershipViolation,
    SurfaceKind,
    TeachingContext,
)

pytestmark = pytest.mark.unit


def _make_ctx(
    *,
    surface_kind: SurfaceKind = SurfaceKind.BROWSER,
    owner: InputOwner = InputOwner.OPERATOR,
    isolation_key: str = "teach:tenant-1:site-a",
) -> TeachingContext:
    return TeachingContext(
        context_id=uuid4(),
        surface_kind=surface_kind,
        isolation_key=isolation_key,
        owner=owner,
        tenant_id=uuid4(),
        site_id="site-a",
    )


class TestOwnerInvariant:
    def test_operator_owner_is_valid(self) -> None:
        ctx = _make_ctx(owner=InputOwner.OPERATOR)
        assert ctx.owner == InputOwner.OPERATOR

    def test_agent_owner_raises_violation(self) -> None:
        """TeachingContext must always be owned by OPERATOR (FR-018)."""
        with pytest.raises(InputOwnershipViolation):
            _make_ctx(owner=InputOwner.AGENT)


class TestConflicts:
    def test_same_isolation_key_conflicts(self) -> None:
        key = "teach:tenant-abc:site-xyz"
        ctx_a = _make_ctx(isolation_key=key)
        ctx_b = _make_ctx(isolation_key=key)
        assert ctx_a.conflicts_with(ctx_b) is True

    def test_different_isolation_key_no_conflict(self) -> None:
        ctx_a = _make_ctx(isolation_key="teach:t1:s1")
        ctx_b = _make_ctx(isolation_key="teach:t1:s2")
        assert ctx_a.conflicts_with(ctx_b) is False


class TestStorageLockKey:
    def test_storage_lock_key_format(self) -> None:
        tid = uuid4()
        ctx = TeachingContext(
            context_id=uuid4(),
            surface_kind=SurfaceKind.BROWSER,
            isolation_key="teach:any",
            owner=InputOwner.OPERATOR,
            tenant_id=tid,
            site_id="checkout",
        )
        assert ctx.storage_lock_key() == f"{tid}:checkout"


class TestInputOwnerTransferProhibited:
    def test_transfer_prohibited_in_teach_mode(self) -> None:
        with pytest.raises(InputOwnershipViolation):
            InputOwner.OPERATOR.transfer_to(InputOwner.AGENT, in_teach_mode=True)

    def test_transfer_outside_teach_mode_does_not_raise(self) -> None:
        """Outside teaching mode, transfer is permitted (no exception)."""
        InputOwner.OPERATOR.transfer_to(InputOwner.AGENT, in_teach_mode=False)
