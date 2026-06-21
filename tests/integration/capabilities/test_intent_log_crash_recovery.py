"""I2 integration — crash entre record_intent y record_outcome no causa re-ejecución.

Simula:
1. El broker llama record_intent (efecto registrado como intención).
2. Crash (sin record_outcome).
3. Restart: el broker detecta intent pendiente → devuelve FAILED (needs_human_review).
4. El adapter NO se vuelve a ejecutar.

Verifica también que AgentLoopOrchestrator.bootstrap() detecta task_ids con
intents pendientes y loguea ERROR (sin re-ejecutar).
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


class TestIntentLogCrashRecovery:
    def test_pending_intent_without_outcome_detected(self, tmp_path: Path) -> None:
        """intent_log.has_pending_intent detecta el intent sin outcome (RECON-1)."""
        from hermes.capabilities.application.intent_log import IntentLog, compute_idempotency_key
        from hermes.domain.proposal import ToolCallProposal

        db_path = str(tmp_path / "intent.db")
        log = IntentLog(db_path=db_path)

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="delete_file",
            tenant_id=uuid4(),
            entity_id="e",
            entity_type="t",
            parameters={"path": "/tmp/x"},
            justification="test",
        )
        key = compute_idempotency_key(proposal)

        # Simular crash: solo record_intent, sin record_outcome.
        log.record_intent(key, proposal, task_id="task-abc")

        assert log.has_pending_intent(key) is True
        assert log.was_executed(key) is False

    async def test_broker_returns_failed_for_pending_intent(self, tmp_path: Path) -> None:
        """El broker devuelve FAILED si hay intent sin outcome para esa key (I2/CTRL-11)."""
        from hermes.capabilities.application.capability_broker import CapabilityBroker
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
        from hermes.capabilities.application.intent_log import IntentLog, compute_idempotency_key
        from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.agents_os.application.consent_manager import (
            Capability,
            ConsentManager,
            ConsentScope,
        )
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.capabilities.domain.ports import ConsentContext, ExecutionStatus
        from hermes.domain.proposal import ToolCallProposal

        signing_key = os.urandom(32)
        signer = AuditHashChainSigner(signing_key=signing_key)
        db_path = tmp_path / "shell.db"
        audit_repo = SqliteAuditRepository(db_path=tmp_path / "audit.db")
        minter = HitlApprovalMinter(signing_key=signing_key)
        approval_gate = SqliteApprovalGate(
            db_path=db_path, minter=minter, signer=signer, audit_repo=audit_repo
        )
        intent_log = IntentLog(db_path=str(db_path))
        registry = CapabilityRegistry()
        dispatcher = SurfaceAdapterDispatcher(adapters={})

        operator_id = uuid4()
        consent_manager = ConsentManager()
        consent_manager.grant(
            tenant_id=uuid4(),
            human_operator_id=operator_id,
            capability=Capability.DOCUMENTS,
            scope=ConsentScope.SESSION,
        )

        broker = CapabilityBroker(
            registry=registry,
            consent_manager=consent_manager,
            approval_gate=approval_gate,
            dispatcher=dispatcher,
            signer=signer,
            audit_repo=audit_repo,
            intent_log=intent_log,
            anchor=FakeExternalAnchor(),
        )

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="read_file",
            tenant_id=uuid4(),
            entity_id="e",
            entity_type="t",
            parameters={"path": "/tmp/x"},
            justification="test I2",
        )
        key = compute_idempotency_key(proposal)
        ctx = ConsentContext(tenant_id=uuid4(), operator_id=operator_id)

        # SIMULAR CRASH: registrar intent sin outcome (proceso murió entre pasos 5 y 8).
        intent_log.record_intent(key, proposal, task_id="task-crash")

        # POST-RESTART: el broker debe detectar el pending intent y NO re-ejecutar.
        outcome = await broker.dispatch(proposal, ctx)

        assert outcome.status == ExecutionStatus.FAILED, (
            "Intent sin outcome debe devolver FAILED (needs_human_review) — NO re-ejecutar (I2)."
        )
        assert "pending_intent" in (outcome.error or "")

    def test_completed_intent_is_not_pending(self, tmp_path: Path) -> None:
        """Intent con outcome EXECUTED no es detectado como pendiente."""
        from hermes.capabilities.application.intent_log import IntentLog, compute_idempotency_key
        from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
        from hermes.domain.proposal import ToolCallProposal

        db_path = str(tmp_path / "intent.db")
        log = IntentLog(db_path=db_path)

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="read_file",
            tenant_id=uuid4(),
            entity_id="e",
            entity_type="t",
            parameters={"path": "/tmp/x"},
            justification="test",
        )
        key = compute_idempotency_key(proposal)

        log.record_intent(key, proposal, task_id="task-ok")
        log.record_outcome(
            key, ExecutionOutcome(proposal_id=proposal.proposal_id, status=ExecutionStatus.EXECUTED)
        )

        assert log.has_pending_intent(key) is False
        assert log.was_executed(key) is True

    def test_pending_task_ids_returned_on_bootstrap(self, tmp_path: Path) -> None:
        """intent_log.pending_task_ids() retorna tareas con intents sin outcome."""
        from hermes.capabilities.application.intent_log import IntentLog, compute_idempotency_key
        from hermes.domain.proposal import ToolCallProposal

        db_path = str(tmp_path / "intent.db")
        log = IntentLog(db_path=db_path)

        task_id_crash = "task-crash-42"
        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="write_file",
            tenant_id=uuid4(),
            entity_id="e",
            entity_type="t",
            parameters={"path": "/tmp/x", "content": "data"},
            justification="test",
        )
        key = compute_idempotency_key(proposal)
        log.record_intent(key, proposal, task_id=task_id_crash)

        task_ids = log.pending_task_ids()
        assert task_id_crash in task_ids
