"""CapabilityBroker — policy_overlay 'approval' axis wiring (item 2, ads-vertical).

Covers the actual HITL-decision seam (_needs_hitl / dispatch), not just the
pure resolver (see test_tool_policy_agent_overlay.py for that):

  - "hitl" override narrows: a would-be auto_executable=True MANAGED_REMOTE
    read still requires HITL when the overlay says so.
  - "auto" override widens: a LOW+not-auto write (e.g. an MCP propose_* tool)
    becomes auto-executable when the overlay explicitly allows it.
  - SECURITY INVARIANT: "auto" can NEVER bypass a HIGH-risk / DANGER /
    MFA-tier tool (tool_delicacy.is_mfa_required), regardless of what the
    overlay says — F-1 (HIGH always HITL) and the MFA-tier guard both run
    BEFORE the override is allowed to widen anything.
  - access_scope_repo=None (Community / not wired) -> no override consulted,
    zero regression (classifier's own auto_executable decision stands).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
from hermes.capabilities.application.capability_broker import CapabilityBroker
from hermes.capabilities.application.intent_log import IntentLog
from hermes.capabilities.domain.agent_access_scope import AgentAccessScope
from hermes.capabilities.domain.ports import (
    CapabilityBinding,
    ConsentContext,
    ExecutionStatus,
    RiskLevel,
)
from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
    SurfaceAdapterDispatcher,
)
from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
from hermes.domain.proposal import ToolCallProposal

pytestmark = pytest.mark.unit

_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()


class _AllowAllConsent:
    """Fake ConsentManagerPort — args unused by design (always-allow stub)."""

    def assert_active(self, **_kwargs: object) -> object:
        return object()

    def use(self, **_kwargs: object) -> object:
        return object()


class _FakeAccessScopeRepo:
    def __init__(self, scope: AgentAccessScope | None) -> None:
        self._scope = scope

    def get_scope(self, *_args: object, **_kwargs: object) -> AgentAccessScope | None:
        """Fake repo — always returns the fixed scope, args unused by design."""
        return self._scope


def _scope_with_overlay(overlay: dict) -> AgentAccessScope:
    return AgentAccessScope.create(
        tenant_id=str(_TENANT_ID), agent_id="agent-a", updated_by=1,
        policy_overlay=overlay,
    )


def _make_broker(
    *,
    access_scope_repo: object | None,
    tool_name: str,
    risk: RiskLevel,
    auto_executable: bool,
) -> tuple[CapabilityBroker, FakeApprovalGate]:
    reg = FakeCapabilityRegistry()
    reg.register(CapabilityBinding(
        tool_name=tool_name, surface_kind=SurfaceKind.MCP_CALL,
        required_capability=None, risk=risk, auto_executable=auto_executable,
        executor="mcp",
    ))
    gate = FakeApprovalGate()
    tmp = Path(tempfile.mkdtemp())
    broker = CapabilityBroker(
        registry=reg,
        consent_manager=_AllowAllConsent(),
        approval_gate=gate,
        dispatcher=SurfaceAdapterDispatcher(adapters={}),
        signer=AuditHashChainSigner(signing_key=b"k" * 32),
        audit_repo=SqliteAuditRepository(db_path=tmp / "audit.db"),
        intent_log=IntentLog(),
        access_scope_repo=access_scope_repo,
        tenant_id=str(_TENANT_ID),
    )
    return broker, gate


def _proposal(tool_name: str) -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT_ID,
        entity_id="test-entity",
        entity_type="test",
        parameters={"op": tool_name},
        justification="approval override test",
    )


def _ctx(*, agent_id: str = "agent-a") -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID, agent_id=agent_id)


class TestApprovalOverrideWidensAutoExecutable:
    @pytest.mark.asyncio
    async def test_auto_override_makes_low_write_auto_executable(self) -> None:
        tool = "mcp__safent-ads__propose_budget_change"
        broker, gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(
                _scope_with_overlay({tool: {"approval": "auto"}})
            ),
            tool_name=tool, risk=RiskLevel.LOW, auto_executable=False,
        )
        outcome = await broker.dispatch(
            _proposal(tool), _ctx(), hitl_approval_token=None,
        )
        # No HITL round-trip needed — it executed straight through (may fail
        # downstream on the missing surface adapter, but MUST NOT be pending).
        assert outcome.status != ExecutionStatus.PENDING_APPROVAL
        assert gate.register_calls == []

    @pytest.mark.asyncio
    async def test_no_override_low_write_stays_pending(self) -> None:
        """Regression baseline: without an overlay, a LOW+not-auto write
        still requires HITL exactly as before this feature."""
        tool = "mcp__safent-ads__propose_budget_change"
        broker, gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(_scope_with_overlay({})),
            tool_name=tool, risk=RiskLevel.LOW, auto_executable=False,
        )
        outcome = await broker.dispatch(
            _proposal(tool), _ctx(), hitl_approval_token=None,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL


class TestApprovalOverrideNarrowsAutoExecutable:
    @pytest.mark.asyncio
    async def test_hitl_override_forces_approval_on_auto_executable_read(self) -> None:
        tool = "mcp__safent-ads__list_campaigns"
        broker, gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(
                _scope_with_overlay({tool: {"approval": "hitl"}})
            ),
            tool_name=tool, risk=RiskLevel.LOW, auto_executable=True,
        )
        outcome = await broker.dispatch(
            _proposal(tool), _ctx(), hitl_approval_token=None,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL


class TestApprovalOverrideNeverBypassesMfaTierOrHigh:
    """SECURITY: 'approval: auto' must NEVER short-circuit HIGH risk or an
    MFA-tier tool — F-1 and the MFA-tier guard both run before the override
    is even consulted for widening."""

    @pytest.mark.asyncio
    async def test_auto_override_cannot_bypass_high_risk(self) -> None:
        tool = "install_mcp"
        broker, gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(
                _scope_with_overlay({tool: {"approval": "auto"}})
            ),
            tool_name=tool, risk=RiskLevel.HIGH, auto_executable=False,
        )
        outcome = await broker.dispatch(
            _proposal(tool), _ctx(), hitl_approval_token=None,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL

    @pytest.mark.asyncio
    async def test_auto_override_cannot_bypass_mfa_tier_tool_even_if_low_risk(self) -> None:
        """Defense in depth: even in the (currently hypothetical) case where
        an MFA-tier tool (tool_delicacy._ENTERPRISE_REVIEW_TOOLS, e.g. install_mcp)
        classified as LOW risk, the approval-override widening must still be
        refused for it."""
        tool = "install_mcp"
        broker, gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(
                _scope_with_overlay({tool: {"approval": "auto"}})
            ),
            tool_name=tool, risk=RiskLevel.LOW, auto_executable=False,
        )
        outcome = await broker.dispatch(
            _proposal(tool), _ctx(), hitl_approval_token=None,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL


class TestApprovalOverrideRegressionWhenUnwired:
    @pytest.mark.asyncio
    async def test_access_scope_repo_none_no_override_consulted(self) -> None:
        tool = "mcp__safent-ads__propose_budget_change"
        broker, gate = _make_broker(
            access_scope_repo=None, tool_name=tool,
            risk=RiskLevel.LOW, auto_executable=False,
        )
        outcome = await broker.dispatch(
            _proposal(tool), _ctx(), hitl_approval_token=None,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL


class TestT194AdsApplyDefensiveActionHonoursApprovalAxis:
    """024/T091/T194: apply_defensive_action solo admite lower_budget|pause y
    pasa por el chokepoint del companion (topes, freno, broker), asi que NO
    es MFA-tier: el eje `approval` del overlay decide. Sin overlay sigue
    siendo HITL (baseline de la jaula); `auto` lo respeta (requisito del
    propietario: pausar/bajar autonomo); `hitl` explicito fuerza tarjeta."""

    @pytest.mark.asyncio
    async def test_without_overlay_stays_hitl(self) -> None:
        tool = "mcp__safent-ads__apply_defensive_action"
        broker, gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(_scope_with_overlay({})),
            tool_name=tool, risk=RiskLevel.LOW, auto_executable=False,
        )
        outcome = await broker.dispatch(
            _proposal(tool), _ctx(), hitl_approval_token=None,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL

    @pytest.mark.asyncio
    async def test_auto_override_is_honoured_for_defensive_actions(self) -> None:
        tool = "mcp__safent-ads__apply_defensive_action"
        broker, gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(
                _scope_with_overlay({tool: {"approval": "auto"}})
            ),
            tool_name=tool, risk=RiskLevel.LOW, auto_executable=False,
        )
        outcome = await broker.dispatch(
            _proposal(tool), _ctx(), hitl_approval_token=None,
        )
        assert outcome.status != ExecutionStatus.PENDING_APPROVAL

    @pytest.mark.asyncio
    async def test_hitl_override_still_forces_approval(self) -> None:
        tool = "mcp__safent-ads__apply_defensive_action"
        broker, gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(
                _scope_with_overlay({tool: {"approval": "hitl"}})
            ),
            tool_name=tool, risk=RiskLevel.LOW, auto_executable=True,
        )
        outcome = await broker.dispatch(
            _proposal(tool), _ctx(), hitl_approval_token=None,
        )
        assert outcome.status == ExecutionStatus.PENDING_APPROVAL


class TestT194ProposeDoesNotRaiseHitlCard:
    """024/T091/T194: propose_*/withdraw_proposal are NOT MFA-tier — they
    only ever create a pending row in the companion's OWN datastore (a
    SEPARATE human approval happens later via Telegram/REST, outside this
    broker), so tool-surface.md's 'auto' policy_overlay entry for them must
    keep working after apply_defensive_action's own tier is tightened."""

    @pytest.mark.asyncio
    async def test_propose_does_not_raise_hitl_card(self) -> None:
        tool = "mcp__safent-ads__propose_budget_change"
        broker, gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(
                _scope_with_overlay({tool: {"approval": "auto"}})
            ),
            tool_name=tool, risk=RiskLevel.LOW, auto_executable=False,
        )
        outcome = await broker.dispatch(
            _proposal(tool), _ctx(), hitl_approval_token=None,
        )
        assert outcome.status != ExecutionStatus.PENDING_APPROVAL
        assert gate.register_calls == []
