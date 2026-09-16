"""Regresión: una ejecución programada que responde o usa herramientas termina COMPLETED.

Antes, cualquier ciclo no-chat sin propuestas nuevas acababa FAILED (`no_actions`)
aunque el agente hubiera consultado herramientas y respondido; la cola lo
reintentaba y el resultado no llegaba a ninguna notificación (14-sep-2026).
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.capabilities.domain.ports import ConsentContext
from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker
from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator, _summary_line
from hermes.tasks.domain.ports import TaskStatus, WorkItem, WorkItemKind
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue
from hermes.testing import FakeReasoningEngine, scripted_response


class _Notifications:
    def __init__(self) -> None:
        self.added: list[dict] = []

    def add(self, **kw) -> None:
        self.added.append(kw)


async def _run_scheduled_cycle(narrative: str) -> tuple[WorkItem, list[dict]]:
    tenant, operator = uuid4(), uuid4()
    queue, notes = InMemoryWorkQueue(), _Notifications()
    orch = AgentLoopOrchestrator(
        queue=queue,
        state=InMemoryAgentState(),
        engine=FakeReasoningEngine([scripted_response(narrative=narrative)]),
        broker=FakeCapabilityBroker(),
        consent_context=ConsentContext(tenant_id=tenant, operator_id=None),
        notify_watchdog=lambda: None,
        notification_store=notes,
    )
    item = WorkItem(
        id=uuid4(),
        tenant_id=tenant,
        trigger_kind="timer",
        kind=WorkItemKind.AUTONOMOUS,
        payload={"enqueued_by": str(operator), "label": "Revisión de campañas"},
    )
    await queue.enqueue(item)
    claimed = await queue.claim_next()
    assert claimed is not None
    await orch._process(claimed)  # noqa: SLF001 — ciclo único, sin run_forever
    return queue._items[item.id], notes.added  # noqa: SLF001


@pytest.mark.asyncio
async def test_scheduled_run_with_answer_is_completed_and_notified() -> None:
    final, notes = await _run_scheduled_cycle("Hay 3 borradores de campaña.")
    assert final.status is TaskStatus.COMPLETED
    ok = [n for n in notes if n.get("status") == "ok"]
    assert len(ok) == 1
    assert ok[0]["title"] == "Tarea 'Revisión de campañas' completada"
    assert ok[0]["body"] == "Hay 3 borradores de campaña."


@pytest.mark.asyncio
async def test_scheduled_run_without_answer_or_actions_still_fails() -> None:
    final, notes = await _run_scheduled_cycle("")
    assert final.status is not TaskStatus.COMPLETED
    assert not [n for n in notes if n.get("status") == "ok"]


def test_summary_line_collapses_and_truncates() -> None:
    assert _summary_line("  dos\n líneas  ") == "dos líneas"
    assert _summary_line("x" * 400).endswith("…") and len(_summary_line("x" * 400)) == 300
