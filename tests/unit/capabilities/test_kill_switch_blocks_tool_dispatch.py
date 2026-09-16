"""025 Top-KILL — one test proving a tool dispatch is refused while the
kill-switch is engaged.

CapabilityBroker.dispatch() Paso 0 (CTRL-12/KILL-2) already checks
AgentStatePort.is_paused() before ANY other effect — this pins that
invariant directly against the REAL broker (not FakeCapabilityBroker), the
exact mechanism the kill-switch REST surface (POST /security/kill-switch)
now fronts.
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
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()
_TOOL = "read_file"


class _AllowAllConsent:
    def assert_active(self, **_kwargs: object) -> object:
        return object()

    def use(self, **_kwargs: object) -> object:
        return object()


def _make_broker(*, agent_state: InMemoryAgentState) -> CapabilityBroker:
    reg = FakeCapabilityRegistry()
    reg.register(CapabilityBinding(
        tool_name=_TOOL, surface_kind=SurfaceKind.FILESYSTEM,
        required_capability=None, risk=RiskLevel.LOW, auto_executable=True,
        executor="native",
    ))
    tmp = Path(tempfile.mkdtemp())
    return CapabilityBroker(
        registry=reg,
        consent_manager=_AllowAllConsent(),
        approval_gate=FakeApprovalGate(),
        dispatcher=SurfaceAdapterDispatcher(adapters={}),
        signer=AuditHashChainSigner(signing_key=b"k" * 32),
        audit_repo=SqliteAuditRepository(db_path=tmp / "audit.db"),
        intent_log=IntentLog(),
        agent_state=agent_state,
        tenant_id=str(_TENANT_ID),
    )


def _proposal() -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=_TOOL,
        tenant_id=_TENANT_ID,
        entity_id="file-1",
        entity_type="file",
        parameters={},
        justification="kill-switch regression",
    )


def _ctx() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID)


class TestToolDispatchRefusedWhileEngaged:
    @pytest.mark.asyncio
    async def test_dispatch_rejected_while_engaged(self) -> None:
        state = InMemoryAgentState(paused=True)
        broker = _make_broker(agent_state=state)

        outcome = await broker.dispatch(_proposal(), _ctx())

        assert outcome.status is ExecutionStatus.REJECTED_BY_POLICY
        assert "kill" in (outcome.error or "").lower() or "paused" in (outcome.error or "").lower()

    @pytest.mark.asyncio
    async def test_dispatch_does_not_cite_kill_switch_once_released(self) -> None:
        """No regression: once released, Paso 0 lets the request through —
        any later rejection (e.g. no adapter registered in this minimal
        fixture) is a DIFFERENT reason, never the kill-switch message."""
        state = InMemoryAgentState(paused=True)
        broker = _make_broker(agent_state=state)
        await state.resume(by=_OPERATOR_ID)

        outcome = await broker.dispatch(_proposal(), _ctx())

        assert "kill-switch" not in (outcome.error or "").lower()
        assert "paused" not in (outcome.error or "").lower()
