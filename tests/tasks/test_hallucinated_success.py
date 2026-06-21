"""Tests anti-éxito-alucinado (CTRL-9/TOP-5) — deben FALLAR antes de T022/T024.

SC-001: proposals=() => tarea FAILED, jamás COMPLETED.
I1: UPDATE a completed sin audit_entry_id => IntegrityError (CHECK nivel SQLite).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from hermes.tasks.domain.ports import TaskStatus, WorkItem

pytestmark = pytest.mark.unit

_TENANT = uuid4()


def _item() -> WorkItem:
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={"instruction": "do thing", "enqueued_by": "op-1"},
    )


# ---------------------------------------------------------------------------
# Unit: InMemory — proposals vacías nunca producen COMPLETED
# ---------------------------------------------------------------------------


class TestNoProposalsNeverCompleted:
    async def test_empty_proposals_marks_failed_not_completed(self) -> None:
        """AgentLoopOrchestrator con proposals=() debe marcar FAILED, nunca COMPLETED."""
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415
        from hermes.testing import FakeReasoningEngine, scripted_response  # noqa: PLC0415
        from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState()
        broker = FakeCapabilityBroker()
        engine = FakeReasoningEngine(scripted=[scripted_response(proposals=())])
        consent = ConsentContext(tenant_id=_TENANT, operator_id=None)

        watchdog_calls: list[None] = []

        orchestrator = AgentLoopOrchestrator(
            queue=queue,
            state=state,
            engine=engine,
            broker=broker,
            consent_context=consent,
            notify_watchdog=lambda: watchdog_calls.append(None),
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        item = _item()
        await queue.enqueue(item)

        await orchestrator.bootstrap()
        # Procesar UN ciclo explícitamente
        claimed = await queue.claim_next()
        assert claimed is not None
        await orchestrator._process(claimed)  # type: ignore[attr-defined]

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        failed = queue.items_with_status(TaskStatus.FAILED) + queue.items_with_status(TaskStatus.PENDING)
        assert len(completed) == 0, "ANTI-ALUCINADO: proposals=() no puede producir COMPLETED"
        assert len(failed) > 0, "proposals=() debe producir FAILED o PENDING (reintento)"

    async def test_completed_only_with_real_evidence(self) -> None:
        """COMPLETED solo si broker.dispatch devuelve EXECUTED + audit_entry_id."""
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415
        from hermes.capabilities.domain.ports import ConsentContext, ExecutionOutcome, ExecutionStatus  # noqa: PLC0415
        from hermes.testing import FakeReasoningEngine, scripted_response  # noqa: PLC0415
        from hermes.domain.proposal import ToolCallProposal  # noqa: PLC0415
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415

        proposal_id = uuid4()
        proposal = ToolCallProposal(
            proposal_id=proposal_id,
            tool_name="read_file",
            tenant_id=_TENANT,
            entity_id="file-1",
            entity_type="file",
            parameters={},
            justification="test",
        )
        audit_id = uuid4()
        outcome = ExecutionOutcome(
            proposal_id=proposal_id,
            status=ExecutionStatus.EXECUTED,
            audit_entry_id=audit_id,
        )

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState()
        broker = FakeCapabilityBroker(
            scripted={proposal_id: outcome}
        )
        engine = FakeReasoningEngine(scripted=[scripted_response(proposals=[proposal])])
        consent = ConsentContext(tenant_id=_TENANT, operator_id=None)

        orchestrator = AgentLoopOrchestrator(
            queue=queue,
            state=state,
            engine=engine,
            broker=broker,
            consent_context=consent,
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        item = _item()
        await queue.enqueue(item)
        await orchestrator.bootstrap()

        claimed = await queue.claim_next()
        assert claimed is not None
        await orchestrator._process(claimed)  # type: ignore[attr-defined]

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        assert len(completed) == 1, "Con evidencia real debe completar"

    async def test_executed_without_audit_entry_id_never_completes(self) -> None:
        """EXECUTED pero sin audit_entry_id (None) => no COMPLETED (SC-001)."""
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415
        from hermes.capabilities.domain.ports import ConsentContext, ExecutionOutcome, ExecutionStatus  # noqa: PLC0415
        from hermes.testing import FakeReasoningEngine, scripted_response  # noqa: PLC0415
        from hermes.domain.proposal import ToolCallProposal  # noqa: PLC0415
        from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415

        proposal_id = uuid4()
        proposal = ToolCallProposal(
            proposal_id=proposal_id,
            tool_name="read_file",
            tenant_id=_TENANT,
            entity_id="file-1",
            entity_type="file",
            parameters={},
            justification="test",
        )
        # EXECUTED pero SIN audit_entry_id — no es evidencia real
        outcome = ExecutionOutcome(
            proposal_id=proposal_id,
            status=ExecutionStatus.EXECUTED,
            audit_entry_id=None,  # <-- el peligro
        )

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState()
        broker = FakeCapabilityBroker(scripted={proposal_id: outcome})
        engine = FakeReasoningEngine(scripted=[scripted_response(proposals=[proposal])])
        consent = ConsentContext(tenant_id=_TENANT, operator_id=None)

        orchestrator = AgentLoopOrchestrator(
            queue=queue,
            state=state,
            engine=engine,
            broker=broker,
            consent_context=consent,
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        item = _item()
        await queue.enqueue(item)
        await orchestrator.bootstrap()

        claimed = await queue.claim_next()
        assert claimed is not None
        await orchestrator._process(claimed)  # type: ignore[attr-defined]

        completed = queue.items_with_status(TaskStatus.COMPLETED)
        assert len(completed) == 0, "EXECUTED sin audit_entry_id no debe completar"


# ---------------------------------------------------------------------------
# Integration: CHECK I1 a nivel SQLite
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestI1AtSqliteLevel:
    async def test_direct_update_completed_without_evidence_raises_integrity_error(
        self, tmp_path
    ) -> None:
        """I1: UPDATE directo en SQLite a completed sin evidencia => IntegrityError."""
        from hermes.tasks.infrastructure.schema import ensure_tasks_schema  # noqa: PLC0415

        db_path = tmp_path / "i1-test.db"
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        ensure_tasks_schema(conn)

        now_iso = datetime.now(tz=UTC).isoformat()
        task_id = str(uuid4())
        future_iso = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
        claim_tok = str(uuid4())
        conn.execute(
            "INSERT INTO agent_tasks "
            "(task_id, trigger_kind, enqueued_by, operator_id, instruction, "
            "status, worker_id, claim_token, claimed_at, lease_expires_at, created_at, updated_at) "
            "VALUES (?, 'manual_enqueue', 'op', 'op', 'do', "
            "'in_progress', 'worker-0', ?, ?, ?, ?, ?)",
            (task_id, claim_tok, now_iso, future_iso, now_iso, now_iso),
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE agent_tasks "
                "SET status='completed', claim_token=NULL, lease_expires_at=NULL "
                "WHERE task_id=?",
                (task_id,),
            )
        conn.close()
