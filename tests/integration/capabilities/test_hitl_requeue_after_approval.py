"""Regression test — HITL re-enqueue after approval (FR-015 / spec 014 inc. 3).

Bug fijado: approve_action nunca llamaba re_enqueue_after_approval, por lo que
la acción aprobada jamás se ejecutaba (el work_item permanecía PENDING_APPROVAL
para siempre).

Cubre:
  1. approve_action re-encola el work_item (PENDING_APPROVAL → PENDING).
  2. El loop, en el re-dispatch, detecta _pending_proposal_id en el payload y
     recupera el token pre-aprobado via approved_token_for(original_proposal_id).
  3. El broker, recibiendo el proposal_id original + token, ejecuta (EXECUTED).
  4. Sin aprobación previa, el re-dispatch sigue devolviendo PENDING_APPROVAL
     (fail-closed intacto).
  5. El token es single-use: un segundo re-dispatch (tras consumo) vuelve a
     PENDING_APPROVAL sin ejecutar.

Condiciones:
  - InMemoryWorkQueue (sin SQLite) para aislar la lógica de dominio.
  - FakeApprovalGate real con verify_token = comparación exacta (no consume,
    para simplificar; la durabilidad la cubre test_hitl_token_durability.py).
  - DbusRuntimeServiceWiring + FakeAgentState.
  - CapabilityBroker + FakeFilesystemAdapter reales.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusRuntimeServiceWiring,
)
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
from hermes.capabilities.testing.fake_approval_gate import FakeApprovalGate
from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor
from hermes.domain.proposal import ToolCallProposal
from hermes.tasks.domain.ports import TaskStatus, WorkItem, WorkItemKind
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

pytestmark = pytest.mark.integration

_SIGNING_KEY = os.urandom(32)
_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()
_APPROVED_BY = uuid4()
_AUTHORIZED_UID = 1000


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
        return ReplayOutcome(action_id=action.action_id, status=ReplayStatus.EXECUTED_OK)

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        return b""


# ---------------------------------------------------------------------------
# Fake state + consent manager
# ---------------------------------------------------------------------------


class _FakeAgentState:
    async def is_paused(self) -> bool:
        return False

    async def pause(self, **_: Any) -> None:
        pass

    async def resume(self, **_: Any) -> None:
        pass


class _FakeConsentManager:
    def assert_active(self, *, human_operator_id: UUID, capability: Any) -> object:
        return object()

    def use(self, *, human_operator_id: UUID, capability: Any) -> object:
        return object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proposal(proposal_id: UUID | None = None) -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=proposal_id or uuid4(),
        tool_name="write_file",
        tenant_id=_TENANT_ID,
        entity_id="test-entity",
        entity_type="test",
        parameters={"op": "write_file", "path": "/tmp/out.txt", "content": "hello"},
        justification="regression test",
    )


def _ctx() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT_ID, operator_id=_OPERATOR_ID)


def _make_pending_work_item(work_item_id: UUID | None = None) -> WorkItem:
    wid = work_item_id or uuid4()
    return WorkItem(
        id=wid,
        tenant_id=_TENANT_ID,
        trigger_kind="manual_enqueue",
        kind=WorkItemKind.AUTONOMOUS,
        priority=0,
        payload={"enqueued_by": str(_OPERATOR_ID), "instruction": "write file"},
        status=TaskStatus.PENDING,
    )


# ---------------------------------------------------------------------------
# Test 1: approve_action re-encola el work_item (PENDING_APPROVAL → PENDING)
# ---------------------------------------------------------------------------


class TestApproveActionReenqueuesWorkItem:
    """Verifica que approve_action llama re_enqueue_after_approval (el callsite faltante)."""

    async def test_approve_transitions_work_item_to_pending(
        self, tmp_path: Path
    ) -> None:
        """approve_action: work_item PENDING_APPROVAL → PENDING (FR-015).

        Antes del fix: el work_item permanecía PENDING_APPROVAL indefinidamente.
        Después del fix: el work_item vuelve a PENDING para que el loop lo drene.
        """
        db_path = tmp_path / "shell-state.db"
        anchor = FakeExternalAnchor()
        audit_repo = SqliteAuditRepository(db_path=db_path, external_anchor=anchor)
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
        gate = SqliteApprovalGate(
            db_path=db_path, minter=minter, signer=signer, audit_repo=audit_repo
        )
        queue = InMemoryWorkQueue()
        fake_state = _FakeAgentState()

        wiring = DbusRuntimeServiceWiring(
            agent_state=fake_state,
            approval_gate=gate,
            authorized_uids=frozenset([_AUTHORIZED_UID]),
            work_queue=queue,
        )

        # Montar un work_item en PENDING_APPROVAL con la propuesta pendiente.
        work_item = _make_pending_work_item()
        proposal = _proposal()

        # Simular que el trabajo fue encolado, reclamado y bloqueado.
        await queue.enqueue(work_item)
        claimed = await queue.claim_next()
        assert claimed is not None, "El work_item debe poder reclamarse"

        await gate.register_pending(
            proposal_id=proposal.proposal_id,
            work_item_id=claimed.id,
            consent_context=_ctx(),
            risk=RiskLevel.HIGH,
            justification="test",
            parameters_redacted={"path": "/tmp/out.txt"},
        )
        await queue.mark_pending_approval(
            claimed.id,
            claim_token=claimed.claim_token,
            proposal_id=proposal.proposal_id,
        )

        # Verificar que está en PENDING_APPROVAL.
        items_pending = queue.items_with_status(TaskStatus.PENDING_APPROVAL)
        assert any(i.id == claimed.id for i in items_pending), (
            "El work_item debe estar en PENDING_APPROVAL antes de aprobar"
        )

        # Ahora el operador aprueba — esto debe re-encolarlo.
        result = await wiring.approve_action(
            proposal_id=proposal.proposal_id,
            sender_uid=_AUTHORIZED_UID,
        )

        assert result.approval_token, "Debe devolver un approval_token"

        # ASSERTION CLAVE: el work_item debe estar ahora en PENDING.
        items_pending_now = queue.items_with_status(TaskStatus.PENDING)
        assert any(i.id == claimed.id for i in items_pending_now), (
            "FR-015: approve_action debe re-encolar el work_item (PENDING_APPROVAL → PENDING). "
            "Antes del fix este assertion fallaba porque re_enqueue_after_approval no se llamaba."
        )

    async def test_approve_stores_pending_proposal_id_in_payload(
        self, tmp_path: Path
    ) -> None:
        """mark_pending_approval persiste _pending_proposal_id en el payload.

        Esto permite al loop recuperar el token pre-aprobado en el re-dispatch.
        """
        db_path = tmp_path / "shell-state.db"
        anchor = FakeExternalAnchor()
        audit_repo = SqliteAuditRepository(db_path=db_path, external_anchor=anchor)
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
        gate = SqliteApprovalGate(
            db_path=db_path, minter=minter, signer=signer, audit_repo=audit_repo
        )
        queue = InMemoryWorkQueue()

        work_item = _make_pending_work_item()
        proposal = _proposal()

        await queue.enqueue(work_item)
        claimed = await queue.claim_next()
        assert claimed is not None

        await queue.mark_pending_approval(
            claimed.id,
            claim_token=claimed.claim_token,
            proposal_id=proposal.proposal_id,
        )

        # El item en PENDING_APPROVAL debe tener _pending_proposal_id en payload.
        pa_items = queue.items_with_status(TaskStatus.PENDING_APPROVAL)
        stored = next((i for i in pa_items if i.id == claimed.id), None)
        assert stored is not None
        assert stored.payload.get("_pending_proposal_id") == str(proposal.proposal_id), (
            "_pending_proposal_id debe persistirse en el payload para que el loop "
            "recupere el token aprobado en el re-dispatch"
        )


# ---------------------------------------------------------------------------
# Test 2: re-dispatch con token pre-aprobado ejecuta (no vuelve a PENDING)
# ---------------------------------------------------------------------------


class TestRedispatchWithPreApprovedToken:
    """Verifica que el broker ejecuta cuando recibe el token pre-aprobado.

    Simula lo que hace el loop al re-dispatchar con el proposal_id original
    + el token aprobado (mecánica de _fetch_pre_approved_token).
    """

    async def test_redispatch_with_preapproved_token_executes(
        self, tmp_path: Path
    ) -> None:
        """Re-dispatch con proposal_id original + token aprobado ⇒ EXECUTED.

        Antes del fix: el re-dispatch sin re-encolar nunca sucedía.
        Después del fix: el loop re-encola, detecta _pending_proposal_id, y el
        broker ejecuta en vez de devolver PENDING_APPROVAL.
        """
        db_path = tmp_path / "shell-state.db"
        anchor = FakeExternalAnchor()
        audit_repo = SqliteAuditRepository(db_path=db_path, external_anchor=anchor)
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
        gate = SqliteApprovalGate(
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

        broker = CapabilityBroker(
            registry=reg,
            consent_manager=_FakeConsentManager(),
            approval_gate=gate,
            dispatcher=dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=IntentLog(),
            anchor=anchor,
        )

        original_proposal = _proposal()
        work_item_id = uuid4()

        # Paso 1: primer dispatch sin token → PENDING.
        outcome1 = await broker.dispatch(
            original_proposal, _ctx(),
            hitl_approval_token=None,
            work_item_id=work_item_id,
        )
        assert outcome1.status == ExecutionStatus.PENDING_APPROVAL
        assert len(adapter.calls) == 0, "No debe ejecutar sin token"

        # Paso 2: operador aprueba → token firmado.
        token = await gate.approve(
            proposal_id=original_proposal.proposal_id,
            approved_by=_APPROVED_BY,
        )

        # Paso 3: el loop re-dispatcha con el proposal_id ORIGINAL + token.
        # Esto simula lo que hace _fetch_pre_approved_token +
        # dataclasses.replace(new_proposal, proposal_id=original_id).
        outcome2 = await broker.dispatch(
            original_proposal, _ctx(),
            hitl_approval_token=token,
            work_item_id=work_item_id,
        )

        assert outcome2.status == ExecutionStatus.EXECUTED, (
            f"Expected EXECUTED, got {outcome2.status}: {outcome2.error}. "
            "El broker debe ejecutar la acción aprobada, no devolver PENDING_APPROVAL."
        )
        assert outcome2.audit_entry_id is not None, "Debe haber audit_entry_id de ejecución real"
        assert len(adapter.calls) == 1, "El adapter debe haber recibido exactamente 1 call"

    async def test_redispatch_without_approval_stays_blocked(
        self, tmp_path: Path
    ) -> None:
        """Sin aprobación, el re-dispatch sigue devolviendo PENDING_APPROVAL.

        Verifica que el fix no debilita el gate (fail-closed intacto).
        """
        db_path = tmp_path / "shell-state.db"
        anchor = FakeExternalAnchor()
        audit_repo = SqliteAuditRepository(db_path=db_path, external_anchor=anchor)
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
        gate = SqliteApprovalGate(
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

        broker = CapabilityBroker(
            registry=reg,
            consent_manager=_FakeConsentManager(),
            approval_gate=gate,
            dispatcher=dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=IntentLog(),
            anchor=anchor,
        )

        proposal = _proposal()
        work_item_id = uuid4()

        outcome1 = await broker.dispatch(
            proposal, _ctx(), hitl_approval_token=None, work_item_id=work_item_id
        )
        assert outcome1.status == ExecutionStatus.PENDING_APPROVAL

        # Re-dispatch sin token (sin aprobación).
        new_proposal = _proposal()  # nuevo proposal_id, como en re-run LLM
        outcome2 = await broker.dispatch(
            new_proposal, _ctx(), hitl_approval_token=None, work_item_id=work_item_id
        )
        assert outcome2.status == ExecutionStatus.PENDING_APPROVAL, (
            "Sin aprobación, el re-dispatch debe seguir BLOQUEADO (fail-closed)"
        )
        assert len(adapter.calls) == 0, "No debe ejecutar sin aprobación"

    async def test_token_single_use_second_redispatch_pending(
        self, tmp_path: Path
    ) -> None:
        """El token es single-use: tras consumirse, un segundo re-dispatch requiere nueva aprobación."""
        db_path = tmp_path / "shell-state.db"
        anchor = FakeExternalAnchor()
        audit_repo = SqliteAuditRepository(db_path=db_path, external_anchor=anchor)
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
        gate = SqliteApprovalGate(
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

        broker = CapabilityBroker(
            registry=reg,
            consent_manager=_FakeConsentManager(),
            approval_gate=gate,
            dispatcher=dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=IntentLog(),
            anchor=anchor,
        )

        proposal = _proposal()
        work_item_id = uuid4()

        # Dispatch → PENDING
        await broker.dispatch(
            proposal, _ctx(), hitl_approval_token=None, work_item_id=work_item_id
        )

        # Aprobar → token
        token = await gate.approve(
            proposal_id=proposal.proposal_id, approved_by=_APPROVED_BY
        )

        # Primer re-dispatch con token → EXECUTED (token consumido)
        outcome_first = await broker.dispatch(
            proposal, _ctx(), hitl_approval_token=token, work_item_id=work_item_id
        )
        assert outcome_first.status == ExecutionStatus.EXECUTED
        assert len(adapter.calls) == 1

        # Segundo re-dispatch con el mismo token → PENDING (token ya consumido)
        # (Usa un nuevo proposal_id para evitar el idempotency_key hit)
        proposal2 = _proposal()
        outcome_second = await broker.dispatch(
            proposal2, _ctx(), hitl_approval_token=token, work_item_id=work_item_id
        )
        assert outcome_second.status == ExecutionStatus.PENDING_APPROVAL, (
            "El token single-use no debe permitir una segunda ejecución sin nueva aprobación"
        )
        assert len(adapter.calls) == 1, "No debe ejecutarse de nuevo con token consumido"


# ---------------------------------------------------------------------------
# Test 3: gate.work_item_id_for_proposal devuelve el mapping correcto
# ---------------------------------------------------------------------------


class TestGateWorkItemIdForProposal:
    """Verifica que SqliteApprovalGate.work_item_id_for_proposal funciona."""

    async def test_returns_work_item_id_after_register(self, tmp_path: Path) -> None:
        db_path = tmp_path / "shell-state.db"
        anchor = FakeExternalAnchor()
        audit_repo = SqliteAuditRepository(db_path=db_path, external_anchor=anchor)
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
        gate = SqliteApprovalGate(
            db_path=db_path, minter=minter, signer=signer, audit_repo=audit_repo
        )

        proposal_id = uuid4()
        work_item_id = uuid4()

        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=work_item_id,
            consent_context=_ctx(),
            risk=RiskLevel.HIGH,
            justification="test",
            parameters_redacted={},
        )

        result = await gate.work_item_id_for_proposal(proposal_id)
        assert result == work_item_id, (
            "work_item_id_for_proposal debe devolver el work_item_id registrado"
        )

    async def test_returns_none_for_unknown_proposal(self, tmp_path: Path) -> None:
        db_path = tmp_path / "shell-state.db"
        anchor = FakeExternalAnchor()
        audit_repo = SqliteAuditRepository(db_path=db_path, external_anchor=anchor)
        signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
        gate = SqliteApprovalGate(
            db_path=db_path, minter=minter, signer=signer, audit_repo=audit_repo
        )

        result = await gate.work_item_id_for_proposal(uuid4())
        assert result is None, "Debe devolver None para un proposal_id desconocido"
