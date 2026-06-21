from __future__ import annotations

from uuid import uuid4

import pytest

from hermes import DecisionContext


def test_requires_trigger() -> None:
    with pytest.raises(ValueError, match="trigger is required"):
        DecisionContext(tenant_id=uuid4(), cycle_id=uuid4(), trigger="")


def test_defaults_are_empty() -> None:
    ctx = DecisionContext(
        tenant_id=uuid4(), cycle_id=uuid4(), trigger="cron.daily"
    )
    assert ctx.subjects == ()
    assert ctx.constraints == {}
    assert ctx.domain_payload == {}
    assert ctx.metadata == {}


def test_created_at_is_set() -> None:
    ctx = DecisionContext(
        tenant_id=uuid4(), cycle_id=uuid4(), trigger="cron.daily"
    )
    assert ctx.created_at is not None
