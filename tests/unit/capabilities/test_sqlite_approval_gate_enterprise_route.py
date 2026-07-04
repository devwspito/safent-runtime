"""SqliteApprovalGate — Enterprise route persistence + approve() guard
(Fase 2 Phase 4b).

Covers:
  - register_pending persists route/sensitivity/agent_id ONLY for
    route="enterprise" rows; a plain (LOCAL, default) register_pending call
    is byte-for-byte unaffected (route/sensitivity/agent_id stay NULL).
  - approve() fail-closed rejects ANY approve attempt on a route='enterprise'
    row (I-1/I-3) — reason='enterprise_route_requires_cloud_decision', no
    token minted, row stays 'pending'.
  - approve() on a LOCAL (route='') row is completely unaffected by the new
    guard (regression: the guard must not fire for the 99% common case).
  - reject() works on an 'enterprise' row exactly like on a LOCAL row (I-2 —
    the guard is scoped to approve() only).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from hermes.capabilities.domain.ports import ConsentContext, RiskLevel
from hermes.capabilities.infrastructure.sqlite_approval_gate import (
    ApprovalGateError,
    SqliteApprovalGate,
)
from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
from hermes.capabilities.tool_sensitivity import SensitivityCategory

pytestmark = pytest.mark.unit

_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()


def _make_gate(tmp_path) -> SqliteApprovalGate:
    signer = MagicMock()
    signer.append = MagicMock()
    signer.append_and_persist = AsyncMock()
    return SqliteApprovalGate(
        db_path=tmp_path / "approvals.db",
        minter=HitlApprovalMinter(signing_key=b"k" * 32),
        signer=signer,
        audit_repo=None,
        mfa_verifier=None,
    )


def _consent() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID)


async def _register(
    gate: SqliteApprovalGate,
    proposal_id,
    *,
    route: str = "",
    sensitivity_categories: frozenset = frozenset(),
    agent_id: str = "",
    tool_name: str = "cronjob",
    action_digest: str = "",
) -> str:
    return await gate.register_pending(
        proposal_id=proposal_id,
        work_item_id=uuid4(),
        consent_context=_consent(),
        risk=RiskLevel.HIGH,
        justification="enterprise route test",
        parameters_redacted={"schedule": "0 9 * * *"},
        tool_name=tool_name,
        action_digest=action_digest or f"dig-{proposal_id}",
        route=route,
        sensitivity_categories=sensitivity_categories,
        agent_id=agent_id,
    )


async def _row(gate: SqliteApprovalGate, proposal_id) -> dict:
    with gate._connect() as conn:  # noqa: SLF001 — test-only introspection
        row = conn.execute(
            "SELECT route, sensitivity, agent_id, status FROM pending_approvals "
            "WHERE proposal_id = ?",
            (str(proposal_id),),
        ).fetchone()
    return dict(row)


class TestRegisterPendingPersistsEnterpriseMetadata:
    @pytest.mark.asyncio
    async def test_enterprise_route_persists_sensitivity_and_agent_id(self, tmp_path) -> None:
        gate = _make_gate(tmp_path)
        pid = uuid4()
        await _register(
            gate, pid,
            route="enterprise",
            sensitivity_categories=frozenset({SensitivityCategory.NEW_EGRESS}),
            agent_id="sales-bot",
        )
        row = await _row(gate, pid)
        assert row["route"] == "enterprise"
        assert json.loads(row["sensitivity"]) == ["new_egress"]
        assert row["agent_id"] == "sales-bot"

    @pytest.mark.asyncio
    async def test_local_route_default_leaves_columns_null(self, tmp_path) -> None:
        """route="" (default) — today's LOCAL path — must be byte-for-byte
        unaffected: route/sensitivity/agent_id all stay NULL."""
        gate = _make_gate(tmp_path)
        pid = uuid4()
        await _register(gate, pid)
        row = await _row(gate, pid)
        assert row["route"] is None
        assert row["sensitivity"] is None
        assert row["agent_id"] is None

    @pytest.mark.asyncio
    async def test_enterprise_route_with_empty_sensitivity_persists_null_sensitivity(
        self, tmp_path
    ) -> None:
        """route='enterprise' but NO sensitivity categories (e.g. eligible only
        via irreversible/MOST_DELICATE) → sensitivity column stays NULL, not '[]'."""
        gate = _make_gate(tmp_path)
        pid = uuid4()
        await _register(gate, pid, route="enterprise", agent_id="agent-a")
        row = await _row(gate, pid)
        assert row["route"] == "enterprise"
        assert row["sensitivity"] is None


class TestApproveGuardsEnterpriseRoute:
    @pytest.mark.asyncio
    async def test_approve_rejects_enterprise_routed_row(self, tmp_path) -> None:
        gate = _make_gate(tmp_path)
        pid = uuid4()
        await _register(gate, pid, route="enterprise", agent_id="agent-a")

        with pytest.raises(ApprovalGateError) as exc_info:
            await gate.approve(proposal_id=pid, approved_by=uuid4())

        assert exc_info.value.reason == "enterprise_route_requires_cloud_decision"
        row = await _row(gate, pid)
        assert row["status"] == "pending"  # untouched — no token minted

    @pytest.mark.asyncio
    async def test_approve_still_works_on_local_route(self, tmp_path) -> None:
        """Regression: the enterprise-route guard must NOT fire for LOCAL rows
        (route="" — the common, unrouted case)."""
        gate = _make_gate(tmp_path)
        pid = uuid4()
        await _register(gate, pid)

        token = await gate.approve(proposal_id=pid, approved_by=uuid4())

        assert token
        row = await _row(gate, pid)
        assert row["status"] == "approved"

    @pytest.mark.asyncio
    async def test_reject_still_works_on_enterprise_routed_row(self, tmp_path) -> None:
        """I-2: the local human can ALWAYS deny, regardless of route — the
        guard is scoped to approve() only."""
        gate = _make_gate(tmp_path)
        pid = uuid4()
        await _register(gate, pid, route="enterprise", agent_id="agent-a")

        await gate.reject(proposal_id=pid, rejected_by=uuid4(), reason="owner denied")

        row = await _row(gate, pid)
        assert row["status"] == "rejected"
