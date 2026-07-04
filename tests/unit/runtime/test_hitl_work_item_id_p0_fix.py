"""Regression tests — P0 HITL approval loop (2026-07).

Bug: a delegated/autonomous cycle (no chat conversation_id) proposes an MCP/
Composio WRITE. The in-cycle dispatch (nous_engine._dispatch_external_write /
_dispatch_write_proposal) called broker.dispatch() WITHOUT a work_item_id, so
register_pending always persisted UUID(int=0). approve_action then read back
work_item_id=0, treated it as "not a queue task" (native-danger path), and
NEVER re-enqueued the work item — the owner's approval had no effect: the
front-end showed "La acción caducó antes de aprobarla — no se ejecutó nada"
and the SAME card kept re-appearing.

Two compounding defects fixed here:
  A. conversation_task_registry now also tracks the REAL work_item_id per
     cycle (mirrors conversation_id) and nous_engine threads it through
     _dispatch_via_bridge -> broker.dispatch(work_item_id=...).
  B. SqliteApprovalGate.register_pending heals a stale work_item_id=0 row once
     a real id arrives, and dbus_runtime_service.approve_action reports
     thread_resumed=True when re_enqueue_after_approval actually succeeds (not
     only when a native-danger thread was signalled).

Plus a durable, DB-backed re-proposal breaker (attempt_count on the pending row)
so a work item that keeps getting re-enqueued/re-claimed without resolution
stops minting fresh PENDING_APPROVAL cards after N attempts.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.dbus_runtime_service import DbusRuntimeServiceWiring
from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
from hermes.capabilities.application.capability_broker import CapabilityBroker
from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
from hermes.capabilities.application.intent_log import IntentLog
from hermes.capabilities.domain.ports import (
    CapabilityBinding,
    ConsentContext,
    ExecutionStatus,
    RiskLevel,
)
from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate
from hermes.capabilities.infrastructure.surface_adapter_dispatcher import SurfaceAdapterDispatcher
from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor
from hermes.domain.proposal import ToolCallProposal
from hermes.domain.tool_spec import ToolRisk, ToolSpec
from hermes.runtime import conversation_task_registry as ctr
from hermes.runtime.nous_engine import GovernedAIAgent, _ExternalToolCatalog
from hermes.tasks.domain.ports import TaskStatus, WorkItem, WorkItemKind
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

pytestmark = pytest.mark.unit

_SIGNING_KEY = os.urandom(32)
_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()
_APPROVED_BY = uuid4()
_AUTHORIZED_UID = 1000


# ---------------------------------------------------------------------------
# Shared fakes (mirrors tests/integration/capabilities/test_hitl_requeue_after_approval.py)
# ---------------------------------------------------------------------------


class _FakeAgentState:
    async def is_paused(self) -> bool:
        return False


class _FakeConsentManager:
    def assert_active(self, *, human_operator_id: UUID, capability: Any) -> object:
        return object()

    def use(self, *, human_operator_id: UUID, capability: Any) -> object:
        return object()


@dataclass
class _FakeMcpAdapter:
    """Records replay() calls; never actually executes anything."""

    calls: list[CapturedAction] = field(default_factory=list)

    async def replay(self, action: CapturedAction) -> ReplayOutcome:
        self.calls.append(action)
        return ReplayOutcome(action_id=action.action_id, status=ReplayStatus.EXECUTED_OK)


def _ctx() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID)


def _make_broker(gate: SqliteApprovalGate, tmp_path: Path, *, tool_name: str = "mcp__pg__pg_advisor") -> tuple[CapabilityBroker, _FakeMcpAdapter, FakeExternalAnchor]:
    anchor = FakeExternalAnchor()
    audit_repo = SqliteAuditRepository(db_path=tmp_path / "audit.db", external_anchor=anchor)
    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    reg = FakeCapabilityRegistry()
    reg.register(CapabilityBinding(
        tool_name=tool_name,
        surface_kind=None,
        required_capability=None,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="mcp",
    ))
    mcp_adapter = _FakeMcpAdapter()
    broker = CapabilityBroker(
        registry=reg,
        consent_manager=_FakeConsentManager(),
        approval_gate=gate,
        dispatcher=SurfaceAdapterDispatcher(adapters={}),
        signer=signer,
        audit_repo=audit_repo,
        intent_log=IntentLog(),
        anchor=anchor,
        mcp_adapter=mcp_adapter,
    )
    return broker, mcp_adapter, anchor


def _make_gate(tmp_path: Path) -> SqliteApprovalGate:
    minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    return SqliteApprovalGate(db_path=tmp_path / "shell-state.db", minter=minter, signer=signer)


def _proposal(tool_name: str = "mcp__pg__pg_advisor", proposal_id: UUID | None = None) -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=proposal_id or uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT_ID,
        entity_id="delegated-task",
        entity_type="mcp",
        parameters={"server_id": "pg", "tool_name": "pg_advisor", "args": {}},
        justification="regression test",
    )


# ---------------------------------------------------------------------------
# Test 1 — broker/MCP proposal with a REAL work_item_id and no native waiter:
# approve_action reports thread_resumed=True (live) via re-enqueue, not via the
# native-danger Event signal (which never fires for this path).
# ---------------------------------------------------------------------------


class TestApproveActionLiveTruthfulForBrokerPath:
    async def test_approve_returns_live_true_via_requeue(self, tmp_path: Path) -> None:
        gate = _make_gate(tmp_path)
        queue = InMemoryWorkQueue()
        wiring = DbusRuntimeServiceWiring(
            agent_state=_FakeAgentState(),
            approval_gate=gate,
            authorized_uids=frozenset([_AUTHORIZED_UID]),
            work_queue=queue,
        )

        work_item = WorkItem(
            id=uuid4(),
            tenant_id=_TENANT_ID,
            trigger_kind="manual_enqueue",
            kind=WorkItemKind.AUTONOMOUS,
            priority=0,
            payload={"enqueued_by": str(_OPERATOR_ID)},
            status=TaskStatus.PENDING,
        )
        await queue.enqueue(work_item)
        claimed = await queue.claim_next()
        assert claimed is not None

        proposal = _proposal()
        # register_pending with the REAL (non-zero) work_item_id — this is what
        # the FIXED in-cycle dispatch now does (Part 1 of the fix).
        await gate.register_pending(
            proposal_id=proposal.proposal_id,
            work_item_id=claimed.id,
            consent_context=_ctx(),
            risk=RiskLevel.HIGH,
            justification="mcp write",
            parameters_redacted={},
            tool_name=proposal.tool_name,
            conversation_id="",  # delegated cycle — no chat thread
        )
        await queue.mark_pending_approval(
            claimed.id, claim_token=claimed.claim_token, proposal_id=proposal.proposal_id,
        )

        # No native-danger Event was ever registered for this proposal (the
        # broker/MCP path never blocks a conversation thread) — signal_native_danger_approval
        # will return False. thread_resumed must still be True because the
        # requeue actually happened.
        result = await wiring.approve_action(proposal_id=proposal.proposal_id, sender_uid=_AUTHORIZED_UID)

        assert result.thread_resumed is True, (
            "approve_action must report live=true when re_enqueue_after_approval "
            "succeeds, even though no chat thread was blocked (broker/MCP path). "
            "Before the fix, thread_resumed only reflected the native-danger "
            "Event signal, which is always False here — causing the "
            "'caducó antes de aprobarla' toast on a task that WILL execute."
        )
        assert any(
            i.id == claimed.id for i in queue.items_with_status(TaskStatus.PENDING)
        ), "the work item must be back in PENDING for the loop to drain it"


# ---------------------------------------------------------------------------
# Test 2 — register_pending heals a stale work_item_id=0 row once a real id
# arrives; never touches a non-pending row or a row that already has a real id.
# ---------------------------------------------------------------------------


class TestRegisterPendingHealsStaleWorkItemId:
    async def test_zero_work_item_id_healed_by_second_call(self, tmp_path: Path) -> None:
        gate = _make_gate(tmp_path)
        proposal_id = uuid4()

        # First registration: no work_item_id available (old in-cycle call site,
        # before the fix) -> broker resolves it to UUID(int=0).
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=UUID(int=0),
            consent_context=_ctx(),
            risk=RiskLevel.HIGH,
            justification="j",
            parameters_redacted={},
            tool_name="mcp__pg__pg_advisor",
        )
        assert await gate.work_item_id_for_proposal(proposal_id) == UUID(int=0)

        # Second registration (e.g. the orchestrator's outer dispatch) carries
        # the REAL work_item_id -> must heal the row in place.
        real_wid = uuid4()
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=real_wid,
            consent_context=_ctx(),
            risk=RiskLevel.HIGH,
            justification="j",
            parameters_redacted={},
            tool_name="mcp__pg__pg_advisor",
        )
        assert await gate.work_item_id_for_proposal(proposal_id) == real_wid, (
            "register_pending must heal a stale work_item_id=0 row once a real "
            "id arrives — otherwise approve_action can never find a queue task "
            "to re-enqueue and the task stays pending_approval forever."
        )

    async def test_approved_unconsumed_row_never_touched(self, tmp_path: Path) -> None:
        gate = _make_gate(tmp_path)
        proposal_id = uuid4()
        real_wid = uuid4()

        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=real_wid,
            consent_context=_ctx(),
            risk=RiskLevel.HIGH,
            justification="j",
            parameters_redacted={},
            tool_name="foo",
        )
        await gate.approve(proposal_id=proposal_id, approved_by=_APPROVED_BY)

        # A later re-registration attempt (e.g. a stray retry) with a DIFFERENT
        # work_item_id must never mutate an approved-and-unconsumed row.
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=uuid4(),
            consent_context=_ctx(),
            risk=RiskLevel.HIGH,
            justification="j",
            parameters_redacted={},
            tool_name="foo",
        )
        assert await gate.work_item_id_for_proposal(proposal_id) == real_wid

    async def test_non_zero_work_item_id_never_overwritten(self, tmp_path: Path) -> None:
        gate = _make_gate(tmp_path)
        proposal_id = uuid4()
        first_wid = uuid4()

        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=first_wid,
            consent_context=_ctx(),
            risk=RiskLevel.HIGH,
            justification="j",
            parameters_redacted={},
            tool_name="bar",
        )
        # A second call with a DIFFERENT non-zero id must NOT overwrite a row
        # that already carries a real id (heal only fires for a zero id).
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=uuid4(),
            consent_context=_ctx(),
            risk=RiskLevel.HIGH,
            justification="j",
            parameters_redacted={},
            tool_name="bar",
        )
        assert await gate.work_item_id_for_proposal(proposal_id) == first_wid

    async def test_heal_does_not_rebind_across_tenants(self, tmp_path: Path) -> None:
        """SECURITY (review Medium/CWE-863): a byte-identical action from a
        DIFFERENT tenant shares the deterministic proposal_id, but the tenant-
        scoped heal must NOT let the other tenant rebind the pending row to its
        own work_item_id (cross-tenant approval confusion)."""
        gate = _make_gate(tmp_path)
        proposal_id = uuid4()
        other_tenant_ctx = ConsentContext(tenant_id=uuid4(), operator_id=_OPERATOR_ID)

        # Tenant A registers first with a task-less (zero) work_item_id.
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=UUID(int=0),
            consent_context=_ctx(),
            risk=RiskLevel.HIGH,
            justification="j",
            parameters_redacted={},
            tool_name="mcp__pg__pg_advisor",
        )
        # Tenant B proposes the identical action (same deterministic proposal_id)
        # carrying its OWN real work_item_id — the heal is tenant-scoped, so it
        # must NOT rebind tenant A's row.
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=uuid4(),
            consent_context=other_tenant_ctx,
            risk=RiskLevel.HIGH,
            justification="j",
            parameters_redacted={},
            tool_name="mcp__pg__pg_advisor",
        )
        assert await gate.work_item_id_for_proposal(proposal_id) == UUID(int=0), (
            "the heal UPDATE must be scoped to the SAME tenant — a different "
            "tenant proposing the same deterministic action must not rebind the "
            "pending row to its own work_item_id."
        )


# ---------------------------------------------------------------------------
# Test 3 — delegated cycle (conversation_id="") proposing an MCP write: the
# proposal surfaces as PENDING (block-and-resume is skipped, matches existing
# non-blocking autonomous behaviour) AND the pending row carries the REAL
# work_item_id of the delegated cycle, not UUID(int=0).
# ---------------------------------------------------------------------------


class TestDelegatedCycleThreadsRealWorkItemId:
    def test_mcp_write_pending_row_carries_delegated_work_item_id(self, tmp_path: Path) -> None:
        work_item_id = uuid4()
        cycle_task_id = "cycle-delegated-1"

        gate = _make_gate(tmp_path)
        broker, mcp_adapter, _anchor = _make_broker(gate, tmp_path)

        spec = ToolSpec(
            name="mcp__pg__pg_advisor",
            description="MCP WRITE",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="mcp",
            handler=None,
        )
        catalog = _ExternalToolCatalog((spec,))

        bg_loop = asyncio.new_event_loop()
        import threading

        t = threading.Thread(target=bg_loop.run_forever, daemon=True)
        t.start()

        fake_inner = MagicMock()
        with patch("hermes.runtime.nous_engine._import_ai_agent") as mock_import:
            mock_import.return_value = MagicMock(return_value=fake_inner)
            agent = GovernedAIAgent(
                model="test/model",
                broker=broker,
                consent_context=_ctx(),
                engine_loop=bg_loop,
                tenant_id=_TENANT_ID,
                external_catalog=catalog,
            )
        agent._inner = fake_inner

        try:
            # Simulate what _run_conversation_with_cdp does at cycle start for a
            # DELEGATED cycle: bind the REAL work_item_id, but NEVER bind a
            # conversation_id (no chat thread — this is the defect-A condition).
            ctr.set_work_item_for_task(cycle_task_id, work_item_id)
            ctr.set_current_cycle_task(cycle_task_id)

            result_str = agent._invoke_tool(
                "mcp__pg__pg_advisor", {"query": "vacuum"}, effective_task_id="",
            )
        finally:
            ctr.clear_work_item_for_task(cycle_task_id)
            ctr.clear_current_cycle_task()
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=3)

        assert "BLOCKED" in result_str, (
            "PENDING_APPROVAL must surface as BLOCKED-and-accumulate for a "
            "delegated cycle (no conversation_id -> non-blocking retry-queue path)"
        )
        assert len(agent._pending_proposals) == 1
        proposal_id = agent._pending_proposals[0].proposal_id

        stored_work_item_id = asyncio.run(gate.work_item_id_for_proposal(proposal_id))
        assert stored_work_item_id == work_item_id, (
            "The pending row must carry the delegated cycle's REAL work_item_id "
            "(not UUID(int=0)) so approve_action can re-enqueue it after the "
            "owner approves — this is the exact defect that caused 'caducó "
            "antes de aprobarla' + the infinite re-card loop."
        )
        assert mcp_adapter.calls == [], "must not execute before approval"


# ---------------------------------------------------------------------------
# Test 4 — durable, DB-backed breaker: re-proposing the SAME (work_item_id,
# tool) proposal past the threshold rejects terminally and STAYS rejected
# (does not reset on the next attempt), instead of re-carding forever.
# ---------------------------------------------------------------------------


class TestDurableReproposalBreaker:
    async def test_breaker_trips_and_stays_tripped(self, tmp_path: Path) -> None:
        gate = _make_gate(tmp_path)
        broker, mcp_adapter, _anchor = _make_broker(gate, tmp_path)

        proposal = _proposal()
        work_item_id = uuid4()

        from hermes.capabilities.infrastructure.sqlite_approval_gate import (
            _MAX_DURABLE_PENDING_ATTEMPTS,
        )

        last_outcome = None
        for _ in range(_MAX_DURABLE_PENDING_ATTEMPTS + 5):
            last_outcome = await broker.dispatch(
                proposal, _ctx(), hitl_approval_token=None, work_item_id=work_item_id,
            )

        assert last_outcome.status is ExecutionStatus.REJECTED_BY_POLICY, (
            "the durable breaker must reject terminally after N re-attempts of "
            "the SAME (work_item_id, tool) proposal without resolution"
        )

        # Sticky: further re-attempts must NOT resurrect it as pending again.
        for _ in range(3):
            outcome = await broker.dispatch(
                proposal, _ctx(), hitl_approval_token=None, work_item_id=work_item_id,
            )
            assert outcome.status is ExecutionStatus.REJECTED_BY_POLICY, (
                "the breaker must stay tripped — a re-registration must not "
                "reset attempt_count and revive the card"
            )

        assert mcp_adapter.calls == [], "a breaker-tripped proposal must never execute"

    async def test_below_threshold_still_pending(self, tmp_path: Path) -> None:
        """Sanity: a normal, slow-but-legitimate approval flow is not falsely tripped."""
        gate = _make_gate(tmp_path)
        broker, _mcp_adapter, _anchor = _make_broker(gate, tmp_path)

        proposal = _proposal()
        work_item_id = uuid4()

        outcome = await broker.dispatch(
            proposal, _ctx(), hitl_approval_token=None, work_item_id=work_item_id,
        )
        assert outcome.status is ExecutionStatus.PENDING_APPROVAL
        outcome = await broker.dispatch(
            proposal, _ctx(), hitl_approval_token=None, work_item_id=work_item_id,
        )
        assert outcome.status is ExecutionStatus.PENDING_APPROVAL, (
            "a couple of re-registrations (in-cycle + orchestrator outer "
            "dispatch, as happens on every legitimate approval) must NOT trip "
            "the durable breaker"
        )


# ---------------------------------------------------------------------------
# Test 5 — TOCTOU (review Info/CWE-367): if the pending row stops being
# 'pending' between _fetch_pending and the approve UPDATE (e.g. the durable
# breaker flips it to 'rejected' concurrently), approve() must abort rather than
# clobber the terminal state back to 'approved'.
# ---------------------------------------------------------------------------


class TestApproveTocTouGuard:
    async def test_approve_aborts_if_row_flipped_after_fetch(self, tmp_path: Path) -> None:
        from hermes.capabilities.infrastructure.sqlite_approval_gate import (
            ApprovalGateError,
        )

        gate = _make_gate(tmp_path)
        proposal_id = uuid4()
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=uuid4(),
            consent_context=_ctx(),
            risk=RiskLevel.HIGH,
            justification="j",
            parameters_redacted={},
            tool_name="foo",
        )

        # Inject a concurrent flip to 'rejected' in the TOCTOU window: the minter
        # runs AFTER _fetch_pending's read but BEFORE the approve UPDATE, so
        # flipping the row here reproduces the durable breaker firing mid-approve.
        orig_mint = gate._minter.mint

        def flipping_mint(**kwargs: Any) -> Any:
            with gate._connect() as conn:
                conn.execute(
                    "UPDATE pending_approvals SET status='rejected' WHERE proposal_id=?",
                    (str(proposal_id),),
                )
            return orig_mint(**kwargs)

        gate._minter.mint = flipping_mint  # type: ignore[method-assign]

        with pytest.raises(ApprovalGateError):
            await gate.approve(proposal_id=proposal_id, approved_by=_APPROVED_BY)

        # The terminal 'rejected' state must survive — never clobbered to approved.
        gate._minter.mint = orig_mint  # type: ignore[method-assign]
        with pytest.raises(ApprovalGateError):
            await gate.approve(proposal_id=proposal_id, approved_by=_APPROVED_BY)


# ---------------------------------------------------------------------------
# Test 6 — several broker/MCP proposals PENDING at once (as when the CEO
# delegates multiple tool-gated tasks): each must get its OWN pending row and be
# approvable. Before the fix every broker row shared action_digest="" and
# collided on the partial UNIQUE index (WHERE status='pending'), so the 2nd
# INSERT OR IGNORE was silently dropped → no row → an unapprovable phantom card.
# Regression for the exact multi-delegation scenario; only reproducible with
# >1 pending proposal at a time (the isolated single-proposal tests miss it,
# which is why the baked-image check caught it).
# ---------------------------------------------------------------------------


class TestConcurrentPendingBrokerProposals:
    async def test_two_pending_broker_proposals_both_get_rows(self, tmp_path: Path) -> None:
        gate = _make_gate(tmp_path)
        p1, p2 = uuid4(), uuid4()
        w1, w2 = uuid4(), uuid4()
        # No action_digest passed (broker/MCP path) — both default to empty.
        await gate.register_pending(
            proposal_id=p1, work_item_id=w1, consent_context=_ctx(),
            risk=RiskLevel.HIGH, justification="j", parameters_redacted={},
            tool_name="mcp__a__x",
        )
        await gate.register_pending(
            proposal_id=p2, work_item_id=w2, consent_context=_ctx(),
            risk=RiskLevel.HIGH, justification="j", parameters_redacted={},
            tool_name="mcp__b__y",
        )
        assert await gate.work_item_id_for_proposal(p1) == w1
        assert await gate.work_item_id_for_proposal(p2) == w2, (
            "the 2nd concurrently-pending broker proposal must get its own row — "
            "before the fix it collided with the 1st on the empty-action_digest "
            "partial unique index and was silently dropped (unapprovable phantom)."
        )
        # Both must be independently approvable.
        await gate.approve(proposal_id=p1, approved_by=_APPROVED_BY)
        await gate.approve(proposal_id=p2, approved_by=_APPROVED_BY)
