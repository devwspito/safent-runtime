"""T-install — Tests for install capability tools (search/install/connect).

Covers:
  (a) install_mcp with no HITL token → PENDING_APPROVAL (HIGH gated, CTRL-1).
  (b) install_mcp with valid HITL token + fake InstallExecutor → EXECUTED.
  (c) Blocked scan result (executor returns blocked=True) → REJECTED_BY_POLICY.
  (d) search_mcp (LOW + auto_executable) → auto-dispatches, no HITL.
  (e) install_executor=None → REJECTED_BY_POLICY for all install tools.
  (f) search_skills / search_apps → EXECUTED (LOW).
  (g) connect_integration (HIGH) → PENDING_APPROVAL without token.

Uses the same fake infrastructure as test_broker_gates.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.application.capability_broker import CapabilityBroker
from hermes.capabilities.application.capability_registry import ExtendedCapabilityBinding
from hermes.capabilities.application.install_executor import InstallExecutorPort
from hermes.capabilities.application.intent_log import IntentLog
from hermes.capabilities.domain.ports import (
    CapabilityBinding,
    ConsentContext,
    ExecutionStatus,
    RiskLevel,
)
from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor
from hermes.domain.proposal import ToolCallProposal

pytestmark = pytest.mark.unit

_SIGNING_KEY = os.urandom(32)
_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _RecordingAdapter:
    _surface_kind: SurfaceKind = SurfaceKind.FILESYSTEM
    _outcome_status: ReplayStatus = ReplayStatus.EXECUTED_OK
    calls: list[CapturedAction] = field(default_factory=list)

    @property
    def surface_kind(self) -> SurfaceKind:
        return self._surface_kind

    async def capture(self, **_: Any) -> CapturedAction:  # pragma: no cover
        raise NotImplementedError

    async def replay(
        self, action: CapturedAction, **_: Any
    ) -> ReplayOutcome:
        self.calls.append(action)
        return ReplayOutcome(action_id=action.action_id, status=self._outcome_status)

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        return b""


class _FakeConsentManager:
    def assert_active(self, *, human_operator_id: UUID, capability: Any) -> object:
        return object()

    def use(self, *, human_operator_id: UUID, capability: Any) -> object:
        return object()


class _FakeInstallExecutor:
    """Scriptable fake InstallExecutorPort."""

    def __init__(
        self,
        *,
        status: ReplayStatus = ReplayStatus.EXECUTED_OK,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self._status = status
        self._result = result or {}
        self._error = error
        self.calls: list[ToolCallProposal] = []

    async def execute(
        self,
        proposal: ToolCallProposal,
        action: CapturedAction,
    ) -> ReplayOutcome:
        self.calls.append(proposal)
        return ReplayOutcome(
            action_id=action.action_id,
            status=self._status,
            result=self._result,
            error=self._error,
        )


# Protocol assertion — checked at import time so mismatches surface early
assert isinstance(_FakeInstallExecutor(), InstallExecutorPort)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_broker(
    *,
    install_executor: InstallExecutorPort | None = None,
    registry: FakeCapabilityRegistry | None = None,
    surface_kind: SurfaceKind = SurfaceKind.FILESYSTEM,
) -> tuple[CapabilityBroker, _RecordingAdapter, FakeApprovalGate]:
    adapter = _RecordingAdapter(_surface_kind=surface_kind)
    reg = registry or FakeCapabilityRegistry()
    gate = FakeApprovalGate()
    anch = FakeExternalAnchor()
    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    consent = _FakeConsentManager()
    intent_log = IntentLog()

    from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
        SurfaceAdapterDispatcher,
    )
    from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
    from pathlib import Path
    import tempfile

    tmp = tempfile.mkdtemp()
    audit_repo = SqliteAuditRepository(db_path=Path(tmp) / "audit.db")

    dispatcher = SurfaceAdapterDispatcher(adapters={surface_kind: adapter})
    broker = CapabilityBroker(
        registry=reg,
        consent_manager=consent,
        approval_gate=gate,
        dispatcher=dispatcher,
        signer=signer,
        audit_repo=audit_repo,
        intent_log=intent_log,
        anchor=anch,
        install_executor=install_executor,
    )
    return broker, adapter, gate


def _reg_low(tool_name: str) -> FakeCapabilityRegistry:
    reg = FakeCapabilityRegistry()
    reg.register(CapabilityBinding(
        tool_name=tool_name,
        surface_kind=None,
        required_capability=None,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="install",
    ))
    return reg


def _reg_high(tool_name: str) -> FakeCapabilityRegistry:
    reg = FakeCapabilityRegistry()
    reg.register(CapabilityBinding(
        tool_name=tool_name,
        surface_kind=None,
        required_capability=None,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="install",
    ))
    return reg


def _proposal(
    tool_name: str,
    parameters: dict[str, Any] | None = None,
) -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT_ID,
        entity_id="test",
        entity_type="test",
        parameters=parameters or {},
        justification="test",
    )


def _ctx(operator_id: UUID | None = _OPERATOR_ID) -> ConsentContext:
    return ConsentContext(
        tenant_id=_TENANT_ID,
        operator_id=operator_id,
    )


# ---------------------------------------------------------------------------
# (a) install_mcp without HITL token → PENDING_APPROVAL
# ---------------------------------------------------------------------------


class TestInstallMcpRequiresHitl:
    async def test_install_mcp_no_token_returns_pending(self) -> None:
        """HIGH tool without token → PENDING_APPROVAL, executor not called (CTRL-1)."""
        fake_exec = _FakeInstallExecutor()
        broker, _adapter, gate = _make_broker(
            install_executor=fake_exec,
            registry=_reg_high("install_mcp"),
        )
        proposal = _proposal("install_mcp", {"server_id": "test", "argv": ["npx", "test"]})
        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert fake_exec.calls == [], "executor MUST NOT be called without HITL token"
        assert proposal.proposal_id in gate.register_calls


# ---------------------------------------------------------------------------
# (b) install_mcp with valid token + fake executor → EXECUTED
# ---------------------------------------------------------------------------


class TestInstallMcpWithToken:
    async def test_install_mcp_with_valid_token_executes(self) -> None:
        """HIGH tool with valid token → EXECUTED; executor is called once (CTRL-1)."""
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter

        minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
        proposal = _proposal("install_mcp", {"server_id": "my-mcp", "argv": ["npx", "-y", "test"]})
        token = minter.mint(proposal_id=proposal.proposal_id, capability="package_manager", ttl=300)

        fake_exec = _FakeInstallExecutor(
            status=ReplayStatus.EXECUTED_OK,
            result={"ok": True, "tool_count": 5},
        )
        gate = FakeApprovalGate()
        gate._approved[proposal.proposal_id] = token

        broker, _adapter, _gate = _make_broker(
            install_executor=fake_exec,
            registry=_reg_high("install_mcp"),
        )
        # Inject gate with the pre-approved token
        object.__setattr__(broker, "_approval_gate", gate)

        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=token)

        assert outcome.status == ExecutionStatus.EXECUTED, f"Expected EXECUTED, got {outcome.status}"
        assert len(fake_exec.calls) == 1
        assert fake_exec.calls[0].tool_name == "install_mcp"


# ---------------------------------------------------------------------------
# (c) Blocked scan result → REJECTED_BY_POLICY
# ---------------------------------------------------------------------------


class TestBlockedScanRejected:
    async def test_blocked_scan_returns_rejected_by_policy(self) -> None:
        """Executor returns REJECTED_BY_POLICY (scan blocked) → REJECTED_BY_POLICY."""
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter

        minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
        proposal = _proposal("install_mcp", {"server_id": "evil", "argv": ["npx", "evil"]})
        token = minter.mint(proposal_id=proposal.proposal_id, capability="package_manager", ttl=300)

        fake_exec = _FakeInstallExecutor(
            status=ReplayStatus.REJECTED_BY_POLICY,
            error="instalación bloqueada por Centro de Seguridad",
        )
        gate = FakeApprovalGate()
        gate._approved[proposal.proposal_id] = token

        broker, _adapter, _gate = _make_broker(
            install_executor=fake_exec,
            registry=_reg_high("install_mcp"),
        )
        object.__setattr__(broker, "_approval_gate", gate)

        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=token)

        assert outcome.status == ExecutionStatus.REJECTED_BY_POLICY
        assert "bloqueada" in (outcome.error or "")


# ---------------------------------------------------------------------------
# (d) search_mcp (LOW + auto_executable) → auto-dispatches without token
# ---------------------------------------------------------------------------


class TestSearchMcpAutoDispatch:
    async def test_search_mcp_low_executes_without_token(self) -> None:
        """LOW + auto_executable=True + executor='install' → EXECUTED, no HITL (CTRL-4)."""
        fake_exec = _FakeInstallExecutor(
            status=ReplayStatus.EXECUTED_OK,
            result={"results": [{"name": "github-mcp"}], "count": 1},
        )
        broker, _adapter, gate = _make_broker(
            install_executor=fake_exec,
            registry=_reg_low("search_mcp"),
        )
        proposal = _proposal("search_mcp", {"query": "github"})
        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)

        assert outcome.status == ExecutionStatus.EXECUTED, f"Got {outcome.status}: {outcome.error}"
        assert len(fake_exec.calls) == 1
        assert gate.register_calls == [], "No PENDING_APPROVAL for LOW auto_executable"

    async def test_search_mcp_does_not_register_pending(self) -> None:
        """LOW search tools must never create PENDING_APPROVAL entries."""
        fake_exec = _FakeInstallExecutor(result={"results": [], "count": 0})
        broker, _adapter, gate = _make_broker(
            install_executor=fake_exec,
            registry=_reg_low("search_mcp"),
        )
        proposal = _proposal("search_mcp", {"query": "postgres"})
        await broker.dispatch(proposal, _ctx())

        assert gate.register_calls == []


# ---------------------------------------------------------------------------
# (e) install_executor=None → REJECTED_BY_POLICY
# ---------------------------------------------------------------------------


class TestInstallExecutorNoneRejected:
    async def test_no_executor_rejects_by_policy(self) -> None:
        """No install_executor injected → REJECTED_BY_POLICY (fail-closed)."""
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter

        minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
        proposal = _proposal("install_mcp", {"server_id": "x", "argv": ["npx", "x"]})
        token = minter.mint(proposal_id=proposal.proposal_id, capability="package_manager", ttl=300)

        gate = FakeApprovalGate()
        gate._approved[proposal.proposal_id] = token

        broker, _adapter, _gate = _make_broker(
            install_executor=None,  # executor deliberately absent
            registry=_reg_high("install_mcp"),
        )
        object.__setattr__(broker, "_approval_gate", gate)

        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=token)

        assert outcome.status == ExecutionStatus.REJECTED_BY_POLICY
        assert "install_executor" in (outcome.error or "")


# ---------------------------------------------------------------------------
# (f) search_skills / search_apps → EXECUTED (LOW)
# ---------------------------------------------------------------------------


class TestSearchSkillsAndApps:
    async def test_search_skills_low_executes(self) -> None:
        fake_exec = _FakeInstallExecutor(result={"results": [], "count": 0})
        broker, _adapter, gate = _make_broker(
            install_executor=fake_exec,
            registry=_reg_low("search_skills"),
        )
        outcome = await broker.dispatch(
            _proposal("search_skills", {"query": "web scraper"}), _ctx()
        )
        assert outcome.status == ExecutionStatus.EXECUTED
        assert gate.register_calls == []

    async def test_search_apps_low_executes(self) -> None:
        fake_exec = _FakeInstallExecutor(result={"results": [], "count": 0})
        broker, _adapter, gate = _make_broker(
            install_executor=fake_exec,
            registry=_reg_low("search_apps"),
        )
        outcome = await broker.dispatch(
            _proposal("search_apps", {"query": "vlc"}), _ctx()
        )
        assert outcome.status == ExecutionStatus.EXECUTED
        assert gate.register_calls == []


# ---------------------------------------------------------------------------
# (g) connect_integration (HIGH) → PENDING_APPROVAL without token
# ---------------------------------------------------------------------------


class TestConnectIntegrationHighGated:
    async def test_connect_integration_no_token_returns_pending(self) -> None:
        """connect_integration is HIGH → always requires HITL (CTRL-1)."""
        fake_exec = _FakeInstallExecutor()
        broker, _adapter, gate = _make_broker(
            install_executor=fake_exec,
            registry=_reg_high("connect_integration"),
        )
        proposal = _proposal("connect_integration", {"slug": "github"})
        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert fake_exec.calls == []
        assert proposal.proposal_id in gate.register_calls
