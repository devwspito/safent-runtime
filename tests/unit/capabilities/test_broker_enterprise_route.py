"""CapabilityBroker Enterprise approval routing (Fase 2 Phase 4e — Part A).

The prior (reverted) attempt read `conversation_task_registry.
get_current_cycle_agent()` inside `_resolve_enterprise_route` — a
threading.local stamped on the cycle's `run_in_executor` WORKER thread, while
`CapabilityBroker.dispatch()` runs on the EVENT-LOOP thread (bridged via
`run_coroutine_threadsafe`) — so it always read "" in the real daemon and the
routing decision was structurally unreachable. This suite proves the FIX: the
broker now uses the EXPLICIT `ConsentContext.agent_id` (resolved by the
caller on ITS OWN thread, see that field's docstring) and NEVER consults the
thread-local at all — verified by patching `get_current_cycle_agent` to blow
up and confirming the broker path is entirely unaffected.

  - A broker-mediated MFA-tier proposal (`tool_delicacy.is_mfa_required`,
    e.g. install_app) for a cloud-managed, remote-approval-enabled agent ->
    register_pending gets route="enterprise" using ONLY the explicit
    consent_context.agent_id.
  - A broker-mediated SIMPLE-tier proposal (send_message) -> route LOCAL,
    unaffected by the tenant gate.
  - access_scope_repo=None (Community / not yet wired) -> everything LOCAL,
    zero regression.
  - A cloud-managed agent WITHOUT remote-approval enabled -> LOCAL.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest

from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.application.capability_broker import CapabilityBroker
from hermes.capabilities.domain.agent_access_scope import AgentAccessScope
from hermes.capabilities.domain.ports import (
    CapabilityBinding,
    ConsentContext,
    ExecutionStatus,
    RiskLevel,
)
from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
from hermes.domain.proposal import ToolCallProposal

pytestmark = pytest.mark.unit

_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()
_ENTERPRISE_ROUTE_MODULE = "hermes.capabilities.infrastructure.enterprise_approval_routing"


class _AllowAllConsent:
    def assert_active(self, *, human_operator_id: object, capability: object) -> object:
        return object()

    def use(self, *, human_operator_id: object, capability: object) -> object:
        return object()


class _FakeAccessScopeRepo:
    def __init__(self, scope: AgentAccessScope | None) -> None:
        self._scope = scope

    def get_scope(self, agent_id: str, tenant_id: str) -> AgentAccessScope | None:
        return self._scope


def _cloud_scope() -> AgentAccessScope:
    return AgentAccessScope.create(
        tenant_id=str(_TENANT_ID), agent_id="agent-a", updated_by=1, managed_by="cloud",
    )


def _make_broker(
    *, access_scope_repo: object | None = None, tool_name: str,
) -> tuple[CapabilityBroker, FakeApprovalGate]:
    reg = FakeCapabilityRegistry()
    reg.register(CapabilityBinding(
        tool_name=tool_name, surface_kind=SurfaceKind.FILESYSTEM,
        required_capability=None, risk=RiskLevel.HIGH, auto_executable=False,
    ))
    gate = FakeApprovalGate()
    from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
    from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
    from hermes.capabilities.application.intent_log import IntentLog
    from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
        SurfaceAdapterDispatcher,
    )
    import tempfile
    from pathlib import Path

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
        justification="broker enterprise route test",
    )


def _ctx(*, agent_id: str = "agent-a") -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID, agent_id=agent_id)


class TestBrokerUsesExplicitAgentIdNeverThreadLocal:
    @pytest.mark.asyncio
    async def test_mfa_tier_routes_enterprise_using_explicit_agent_id(self) -> None:
        broker, gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(_cloud_scope()), tool_name="install_app",
        )
        proposal = _proposal("install_app")

        with (
            patch(
                f"{_ENTERPRISE_ROUTE_MODULE}.tenant_remote_approval_enabled",
                return_value=True,
            ),
            patch(
                "hermes.runtime.conversation_task_registry.get_current_cycle_agent",
                side_effect=AssertionError(
                    "broker path must NEVER consult the thread-local — "
                    "it must use ConsentContext.agent_id explicitly"
                ),
            ),
        ):
            outcome = await broker.dispatch(
                proposal, _ctx(agent_id="agent-a"), hitl_approval_token=None,
            )

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert proposal.proposal_id in gate.register_calls
        entry = gate._pending[proposal.proposal_id]
        assert entry["route"] == "enterprise"
        assert entry["agent_id"] == "agent-a"

    @pytest.mark.asyncio
    async def test_get_current_cycle_agent_is_never_called_by_broker_path(self) -> None:
        """Direct regression test for the reverted attempt's root-cause bug:
        the broker must resolve routing without ever touching the ambient
        thread-local, regardless of tool tier or tenant gate state."""
        broker, _gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(_cloud_scope()), tool_name="install_app",
        )
        proposal = _proposal("install_app")

        with (
            patch(
                f"{_ENTERPRISE_ROUTE_MODULE}.tenant_remote_approval_enabled",
                return_value=True,
            ),
            patch(
                "hermes.runtime.conversation_task_registry.get_current_cycle_agent"
            ) as mock_thread_local,
        ):
            await broker.dispatch(proposal, _ctx(agent_id="agent-a"), hitl_approval_token=None)

        mock_thread_local.assert_not_called()


class TestBrokerSimpleTierStaysLocal:
    @pytest.mark.asyncio
    async def test_simple_tier_routes_local_even_cloud_gated(self) -> None:
        broker, gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(_cloud_scope()), tool_name="send_message",
        )
        proposal = _proposal("send_message")

        with patch(
            f"{_ENTERPRISE_ROUTE_MODULE}.tenant_remote_approval_enabled", return_value=True,
        ):
            outcome = await broker.dispatch(
                proposal, _ctx(agent_id="agent-a"), hitl_approval_token=None,
            )

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        entry = gate._pending[proposal.proposal_id]
        assert entry["route"] == ""
        assert entry["agent_id"] == ""


class TestBrokerRegressionWhenUnwiredOrUngated:
    @pytest.mark.asyncio
    async def test_access_scope_repo_none_stays_local_even_for_mfa_tier_tool(self) -> None:
        broker, gate = _make_broker(access_scope_repo=None, tool_name="install_app")
        proposal = _proposal("install_app")

        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        entry = gate._pending[proposal.proposal_id]
        assert entry["route"] == ""

    @pytest.mark.asyncio
    async def test_cloud_managed_without_remote_approval_stays_local(self) -> None:
        broker, gate = _make_broker(
            access_scope_repo=_FakeAccessScopeRepo(_cloud_scope()), tool_name="install_app",
        )
        proposal = _proposal("install_app")

        with patch(
            f"{_ENTERPRISE_ROUTE_MODULE}.tenant_remote_approval_enabled", return_value=False,
        ):
            outcome = await broker.dispatch(
                proposal, _ctx(agent_id="agent-a"), hitl_approval_token=None,
            )

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        entry = gate._pending[proposal.proposal_id]
        assert entry["route"] == ""
