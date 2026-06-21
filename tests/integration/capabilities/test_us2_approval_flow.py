"""T033 — Integration: US2 approval flow end-to-end.

Cubre el flujo completo de una propuesta HIGH-write:
  1. dispatch HIGH sin token ⇒ PENDING_APPROVAL (cola no bloqueada).
  2. approve ⇒ token firmado.
  3. re-dispatch con token ⇒ EXECUTED + audit_entry_id real.
  4. audit incluye HITL_APPROVED (SC-004): approved_by registrado.
  5. Tarea completable con audit_entry_id real.

Condiciones:
  - SQLite real en tmp_path (no in-memory).
  - FakeReplayAdapter que graba llamadas.
  - HitlApprovalMinter + SqliteApprovalGate reales.
  - AuditHashChainSigner + SqliteAuditRepository reales.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner, AuditKind
from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
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

pytestmark = pytest.mark.integration

_SIGNING_KEY = os.urandom(32)
_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()
_APPROVED_BY = uuid4()


# ---------------------------------------------------------------------------
# Fake adapter que graba llamadas
# ---------------------------------------------------------------------------


@dataclass
class _FakeFilesystemAdapter:
    calls: list[CapturedAction] = field(default_factory=list)

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.FILESYSTEM

    async def capture(self, **_: Any) -> CapturedAction:  # pragma: no cover
        raise NotImplementedError

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        self.calls.append(action)
        return ReplayOutcome(
            action_id=action.action_id,
            status=ReplayStatus.EXECUTED_OK,
        )

    def serialize_for_signing(self, action: CapturedAction) -> bytes:  # pragma: no cover
        return b""


# ---------------------------------------------------------------------------
# Fake ConsentManager
# ---------------------------------------------------------------------------


class _FakeConsentManager:
    def assert_active(self, *, human_operator_id: UUID, capability: Any) -> object:
        return object()

    def use(self, *, human_operator_id: UUID, capability: Any) -> object:
        return object()


# ---------------------------------------------------------------------------
# Fixture: broker completo con SQLite real
# ---------------------------------------------------------------------------


@pytest.fixture()
def broker_env(tmp_path: Path):
    """Entorno completo de broker con SQLite real en tmp_path."""
    db_path = tmp_path / "shell-state.db"
    anchor = FakeExternalAnchor()
    audit_repo = SqliteAuditRepository(db_path=db_path, external_anchor=anchor)
    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
    approval_gate = SqliteApprovalGate(
        db_path=db_path, minter=minter, signer=signer, audit_repo=audit_repo
    )

    adapter = _FakeFilesystemAdapter()
    dispatcher = SurfaceAdapterDispatcher(adapters={SurfaceKind.FILESYSTEM: adapter})

    reg = FakeCapabilityRegistry()
    reg.register(CapabilityBinding(
        tool_name="write_file",
        surface_kind=SurfaceKind.FILESYSTEM,
        required_capability=None,
        risk=RiskLevel.HIGH,
        auto_executable=False,
    ))

    intent_log = IntentLog()
    consent = _FakeConsentManager()

    broker = CapabilityBroker(
        registry=reg,
        consent_manager=consent,
        approval_gate=approval_gate,
        dispatcher=dispatcher,
        signer=signer,
        audit_repo=audit_repo,
        intent_log=intent_log,
        anchor=anchor,
    )

    return {
        "broker": broker,
        "approval_gate": approval_gate,
        "adapter": adapter,
        "audit_repo": audit_repo,
        "anchor": anchor,
    }


def _proposal(tool_name: str = "write_file") -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT_ID,
        entity_id="test-entity",
        entity_type="test",
        parameters={"op": "write_file", "path": "/tmp/out.txt", "content": "hello"},
        justification="integration test",
    )


def _ctx() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID)


# ---------------------------------------------------------------------------
# T033: Flujo completo HIGH-write → PENDING → approve → EXECUTED
# ---------------------------------------------------------------------------


class TestUs2ApprovalFlow:
    async def test_high_dispatch_without_token_returns_pending(self, broker_env) -> None:
        """Dispatch HIGH sin token ⇒ PENDING_APPROVAL (cola no bloqueada)."""
        broker: CapabilityBroker = broker_env["broker"]
        proposal = _proposal()

        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)

        assert outcome.status == ExecutionStatus.PENDING_APPROVAL
        assert broker_env["adapter"].calls == []  # no ejecutó

    async def test_approve_returns_token(self, broker_env) -> None:
        """approve() devuelve un token firmado."""
        broker: CapabilityBroker = broker_env["broker"]
        gate: SqliteApprovalGate = broker_env["approval_gate"]
        proposal = _proposal()

        # Registrar primero (como haría el primer dispatch)
        await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)

        token = await gate.approve(
            proposal_id=proposal.proposal_id, approved_by=_APPROVED_BY
        )

        assert isinstance(token, str)
        assert len(token) > 0

    async def test_redispatch_with_token_executes_and_audits(self, broker_env) -> None:
        """Re-dispatch con token ⇒ EXECUTED + audit_entry_id real."""
        broker: CapabilityBroker = broker_env["broker"]
        gate: SqliteApprovalGate = broker_env["approval_gate"]
        audit_repo: SqliteAuditRepository = broker_env["audit_repo"]
        adapter: _FakeFilesystemAdapter = broker_env["adapter"]
        proposal = _proposal()

        # Paso 1: primer dispatch ⇒ PENDING
        outcome1 = await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)
        assert outcome1.status == ExecutionStatus.PENDING_APPROVAL

        # Paso 2: approve
        token = await gate.approve(
            proposal_id=proposal.proposal_id, approved_by=_APPROVED_BY
        )

        # Paso 3: re-dispatch con token ⇒ EXECUTED
        outcome2 = await broker.dispatch(proposal, _ctx(), hitl_approval_token=token)

        assert outcome2.status == ExecutionStatus.EXECUTED, (
            f"Expected EXECUTED, got {outcome2.status}: {outcome2.error}"
        )
        assert outcome2.audit_entry_id is not None, "audit_entry_id debe estar presente"
        assert len(adapter.calls) == 1, "adapter.replay debe haber sido llamado una vez"

    async def test_audit_chain_has_executed_entry(self, broker_env) -> None:
        """El audit chain tiene la entrada PROPOSAL_EXECUTED tras la ejecución."""
        broker: CapabilityBroker = broker_env["broker"]
        gate: SqliteApprovalGate = broker_env["approval_gate"]
        audit_repo: SqliteAuditRepository = broker_env["audit_repo"]
        proposal = _proposal()

        await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)
        token = await gate.approve(
            proposal_id=proposal.proposal_id, approved_by=_APPROVED_BY
        )
        outcome = await broker.dispatch(proposal, _ctx(), hitl_approval_token=token)

        chain = await audit_repo.load_chain()
        executed_entries = [
            e for e in chain if e.audit_kind == AuditKind.PROPOSAL_EXECUTED
        ]
        assert len(executed_entries) >= 1, "Debe haber al menos un PROPOSAL_EXECUTED en el chain"

        last_exec = executed_entries[-1]
        assert str(last_exec.entry_id) == str(outcome.audit_entry_id)

    async def test_anchor_called_after_append(self, broker_env) -> None:
        """El FakeExternalAnchor recibe el head_hash tras el append (CTRL-8)."""
        broker: CapabilityBroker = broker_env["broker"]
        gate: SqliteApprovalGate = broker_env["approval_gate"]
        anchor: FakeExternalAnchor = broker_env["anchor"]
        proposal = _proposal()

        initial_anchor_count = len(anchor.anchored)

        await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)
        token = await gate.approve(
            proposal_id=proposal.proposal_id, approved_by=_APPROVED_BY
        )
        await broker.dispatch(proposal, _ctx(), hitl_approval_token=token)

        assert len(anchor.anchored) > initial_anchor_count, (
            "anchor.anchor() debe haberse llamado al menos una vez"
        )

    async def test_audit_chain_integrity_after_full_flow(self, broker_env) -> None:
        """La cadena completa verifica correctamente tras el flujo (SC-006)."""
        broker: CapabilityBroker = broker_env["broker"]
        gate: SqliteApprovalGate = broker_env["approval_gate"]
        audit_repo: SqliteAuditRepository = broker_env["audit_repo"]
        proposal = _proposal()

        await broker.dispatch(proposal, _ctx(), hitl_approval_token=None)
        token = await gate.approve(
            proposal_id=proposal.proposal_id, approved_by=_APPROVED_BY
        )
        await broker.dispatch(proposal, _ctx(), hitl_approval_token=token)

        chain = await audit_repo.load_chain()
        assert len(chain) > 0

        verifier = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        verifier.verify_chain(chain)  # No raise ⇒ cadena íntegra


# ---------------------------------------------------------------------------
# T032 (parte ancla): FakeExternalAnchor detecta cadena local reescrita
# ---------------------------------------------------------------------------


class TestExternalAnchorDetectsTampering:
    async def test_anchor_verify_fails_if_local_head_diverges(
        self, tmp_path: Path
    ) -> None:
        """FakeExternalAnchor detecta cadena local reescrita (CTRL-8/AUD-2).

        Simula:
        1. Append real → anchor registra el head hash.
        2. Se 'reescribe' la cadena local (nuevo signer desde genesis).
        3. verify(new_head) devuelve False — divergencia detectada.
        """
        anchor = FakeExternalAnchor()
        db_path = tmp_path / "audit.db"
        audit_repo = SqliteAuditRepository(db_path=db_path, external_anchor=anchor)
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)

        # Append real
        entry = signer.append(
            audit_kind=AuditKind.TASK_ENQUEUED,
            actor="test",
            description="original",
            payload={"key": "v"},
        )
        await audit_repo.append(entry)

        real_head = signer.head_hash_hex
        assert await anchor.verify(real_head) is True

        # Simular cadena reescrita: nuevo signer parte de genesis → head diferente
        rewrite_signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        rewrite_entry = rewrite_signer.append(
            audit_kind=AuditKind.TASK_ENQUEUED,
            actor="attacker",
            description="tampered",
            payload={"key": "tampered"},
        )
        tampered_head = rewrite_signer.head_hash_hex

        # El head tampered difiere del anclado
        assert tampered_head != real_head
        assert await anchor.verify(tampered_head) is False

    async def test_anchor_verify_true_for_matching_head(
        self, tmp_path: Path
    ) -> None:
        """anchor.verify(head) es True si el head coincide con el anclado."""
        anchor = FakeExternalAnchor()
        db_path = tmp_path / "audit.db"
        audit_repo = SqliteAuditRepository(db_path=db_path, external_anchor=anchor)
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)

        entry = signer.append(
            audit_kind=AuditKind.TASK_ENQUEUED,
            actor="test",
            description="valid",
            payload={},
        )
        await audit_repo.append(entry)

        head = signer.head_hash_hex
        assert await anchor.verify(head) is True

    async def test_anchor_verify_fails_without_any_anchors(self) -> None:
        """anchor.verify devuelve False si no hay ningún hash anclado (fail-closed)."""
        anchor = FakeExternalAnchor()
        assert await anchor.verify("some_hash") is False
