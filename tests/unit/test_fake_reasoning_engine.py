from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from hermes import DecisionContext, ToolCallProposal
from hermes.testing import FakeReasoningEngine, scripted_response

_TENANT = UUID("00000000-0000-0000-0000-000000000099")


def _make_proposal(name: str = "presentar_303") -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=name,
        tenant_id=_TENANT,
        entity_id="12345678Z",
        entity_type="cliente",
        parameters={},
        justification="",
    )


@pytest.mark.asyncio
async def test_returns_scripted_responses_in_order() -> None:
    proposal = _make_proposal()
    engine = FakeReasoningEngine(
        scripted=[
            scripted_response(narrative="first"),
            scripted_response(narrative="second", proposals=[proposal]),
        ],
    )
    ctx = DecisionContext(tenant_id=_TENANT, cycle_id=uuid4(), trigger="cron")

    first = await engine.run_cycle(ctx)
    assert first.narrative == "first"
    assert first.tool_call_proposals == ()

    second = await engine.run_cycle(ctx)
    assert second.narrative == "second"
    assert second.tool_call_proposals == (proposal,)


@pytest.mark.asyncio
async def test_default_returns_empty_cycle() -> None:
    engine = FakeReasoningEngine()
    ctx = DecisionContext(tenant_id=_TENANT, cycle_id=uuid4(), trigger="cron")
    out = await engine.run_cycle(ctx)
    assert out.tool_call_proposals == ()
    assert out.narrative == ""


@pytest.mark.asyncio
async def test_records_calls() -> None:
    engine = FakeReasoningEngine()
    ctx = DecisionContext(tenant_id=_TENANT, cycle_id=uuid4(), trigger="cron")
    await engine.run_cycle(ctx)
    assert engine.calls == [ctx]
