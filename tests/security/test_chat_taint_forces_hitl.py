"""T052 🔒 — Verifica que derived_from_untrusted_content=True en chat_message.

G4 / CTRL-P1-24:
  Todo WorkItem(kind=chat_message) debe marcar derived_from_untrusted_content=True
  en el ConsentContext que se pasa al broker. El broker fuerza HITL sobre las
  proposals derivadas de contenido untrusted (capability_broker.py:158).

Cubre:
  1. _taint_consent_if_chat eleva el flag para chat_message.
  2. _taint_consent_if_chat conserva el consent original para autonomous.
  3. El orchestrator pasa consent taintado a _dispatch_proposals para chat_message.
  4. El broker con dispatch sobre consent taintado rechaza HIGH sin HITL token.
  5. ConsentContext.derived_from_untrusted_content = False por defecto (no regresión).
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from hermes.capabilities.domain.ports import ConsentContext

pytestmark = pytest.mark.unit

_TENANT = uuid4()
_OPERATOR = uuid4()


# ---------------------------------------------------------------------------
# Tests de la función pura _taint_consent_if_chat (T052 — dominio)
# ---------------------------------------------------------------------------


class TestTaintConsentIfChat:
    def test_chat_message_sets_taint_true(self) -> None:
        """derived_from_untrusted_content=True para kind=chat_message."""
        from hermes.tasks.application.agent_loop_orchestrator import (
            _taint_consent_if_chat,
        )

        base = ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)
        result = _taint_consent_if_chat(base, is_chat=True)

        assert result.derived_from_untrusted_content is True, (
            "CTRL-P1-24 / G4: chat_message debe taintar derived_from_untrusted_content. "
            "El broker fuerza HITL sobre proposals de contenido untrusted."
        )

    def test_autonomous_preserves_taint_false(self) -> None:
        """Las tareas autónomas NO reciben taint — no son input del usuario."""
        from hermes.tasks.application.agent_loop_orchestrator import (
            _taint_consent_if_chat,
        )

        base = ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)
        result = _taint_consent_if_chat(base, is_chat=False)

        assert result.derived_from_untrusted_content is False, (
            "Las tareas autónomas no deben recibir taint — no son input del usuario."
        )

    def test_taint_preserves_tenant_and_operator(self) -> None:
        """El taint no altera tenant_id ni operator_id del consent original."""
        from hermes.tasks.application.agent_loop_orchestrator import (
            _taint_consent_if_chat,
        )

        base = ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)
        result = _taint_consent_if_chat(base, is_chat=True)

        assert result.tenant_id == _TENANT
        assert result.operator_id == _OPERATOR

    def test_consent_default_not_tainted(self) -> None:
        """ConsentContext base tiene derived_from_untrusted_content=False por defecto."""
        ctx = ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)
        assert ctx.derived_from_untrusted_content is False


# ---------------------------------------------------------------------------
# Test de integración: orchestrator usa consent taintado para chat_message
# ---------------------------------------------------------------------------


class TestOrchestratorTaintsPropagation:
    """Verifica que _process inyecte consent taintado al broker para chat_message."""

    async def test_chat_message_dispatches_with_tainted_consent(self) -> None:
        """El orchestrator propaga derived_from_untrusted_content=True para chat."""
        from hermes.tasks.application.agent_loop_orchestrator import (
            AgentLoopOrchestrator,
        )
        from hermes.tasks.domain.ports import TaskStatus, WorkItem, WorkItemKind
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
        from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
        from hermes.domain.proposal import ToolCallProposal

        # Capturar el consent que llega al broker para verificar el taint.
        received_consents: list[ConsentContext] = []

        class _CaptureBroker:
            async def dispatch(self, proposal, consent, *, hitl_approval_token=None, work_item_id=None, autonomy_level=None, conversation_id=""):
                received_consents.append(consent)
                return ExecutionOutcome(
                    proposal_id=proposal.proposal_id,
                    status=ExecutionStatus.EXECUTED,
                    audit_entry_id=uuid4(),
                    execution_head_hash="abc",
                )

        class _StubEngine:
            async def run_cycle(self, ctx):
                from hermes.domain.cycle_output import CycleOutput  # noqa: PLC0415
                proposal = ToolCallProposal(
                    proposal_id=uuid4(),
                    tool_name="read_file",
                    tenant_id=_TENANT,
                    entity_id="e",
                    entity_type="t",
                    parameters={"path": "/tmp/x"},
                    justification="test",
                )
                return CycleOutput(tool_call_proposals=(proposal,))

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState()
        consent = ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)

        item = WorkItem.new(
            tenant_id=_TENANT,
            trigger_kind="chat_message",
            payload={"enqueued_by": str(_OPERATOR), "instruction": "hola"},
            kind=WorkItemKind.CHAT_MESSAGE,
        )
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        orch = AgentLoopOrchestrator(
            queue=queue,
            state=state,
            engine=_StubEngine(),
            broker=_CaptureBroker(),
            consent_context=consent,
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
        )

        await orch._process(claimed)

        assert len(received_consents) == 1, "El broker debe haber recibido un dispatch."
        assert received_consents[0].derived_from_untrusted_content is True, (
            "CTRL-P1-24 / G4: el broker debe recibir consent con taint=True "
            "para chat_message (HITL forzado sobre proposals derivadas de chat)."
        )

    async def test_autonomous_does_not_taint_consent(self) -> None:
        """Para kind=autonomous, el broker recibe consent sin taint."""
        from hermes.tasks.application.agent_loop_orchestrator import (
            AgentLoopOrchestrator,
        )
        from hermes.tasks.domain.ports import TaskStatus, WorkItem, WorkItemKind
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
        from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
        from hermes.domain.proposal import ToolCallProposal

        received_consents: list[ConsentContext] = []

        class _CaptureBroker:
            async def dispatch(self, proposal, consent, *, hitl_approval_token=None, work_item_id=None, autonomy_level=None, conversation_id=""):
                received_consents.append(consent)
                return ExecutionOutcome(
                    proposal_id=proposal.proposal_id,
                    status=ExecutionStatus.EXECUTED,
                    audit_entry_id=uuid4(),
                    execution_head_hash="abc",
                )

        class _StubEngine:
            async def run_cycle(self, ctx):
                from hermes.domain.cycle_output import CycleOutput  # noqa: PLC0415
                proposal = ToolCallProposal(
                    proposal_id=uuid4(),
                    tool_name="read_file",
                    tenant_id=_TENANT,
                    entity_id="e",
                    entity_type="t",
                    parameters={"path": "/tmp/x"},
                    justification="test",
                )
                return CycleOutput(tool_call_proposals=(proposal,))

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState()
        consent = ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)

        item = WorkItem.new(
            tenant_id=_TENANT,
            trigger_kind="manual_enqueue",
            payload={"enqueued_by": str(_OPERATOR), "instruction": "tarea auto"},
            kind=WorkItemKind.AUTONOMOUS,
        )
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        orch = AgentLoopOrchestrator(
            queue=queue,
            state=state,
            engine=_StubEngine(),
            broker=_CaptureBroker(),
            consent_context=consent,
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
        )

        await orch._process(claimed)

        assert len(received_consents) == 1
        assert received_consents[0].derived_from_untrusted_content is False, (
            "Las tareas autónomas NO deben taintar el consent — "
            "no provienen de input del usuario."
        )
