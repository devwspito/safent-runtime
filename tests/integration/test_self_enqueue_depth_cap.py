"""Tests FIRST — self_enqueue depth cap (SC-007, FR-022, CTRL-P2-10).

Una tarea hija (auto-encolada) NO puede auto-encolarse a su vez (cap=1).
La bomba de tareas queda acotada en la primera generación.

Cubre:
  - Tarea madre sin cascade_depth → hija encolada (profundidad 0→1)
  - Tarea hija con cascade_depth=1 → rechazo (bomba acotada)
  - dedup_key obligatorio en self_enqueue (None → rechazo)
  - presupuesto/hora: 6 por hora por origen (budget agotado → rechazo)
  - enqueued_by de la hija heredado de la madre
"""
from __future__ import annotations

import pytest
from uuid import UUID, uuid4

from hermes.tasks.triggers.domain.authorized_trigger_ports import (
    AuthorizedTriggerType,
    RiskCeiling,
)
from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (
    SqliteAuthorizedTriggerRepository,
)
from hermes.tasks.triggers.application.trigger_gate import TriggerGate
from hermes.tasks.triggers.application.self_enqueue_source import SelfEnqueueSource
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
from hermes.tasks.domain.ports import WorkItemKind, TaskStatus, WorkItem
from hermes.agents_os.application.audit_hash_chain import AuditKind


ADMIN_UUID = UUID("00000000-0000-0000-0000-000000000001")
TENANT_ID = UUID("00000000-0000-0000-0000-000000000099")
FAKE_SIG = "sha256:fake-test-sig"


def _make_gate(
    repo: SqliteAuthorizedTriggerRepository,
    queue: InMemoryWorkQueue,
) -> TriggerGate:
    return TriggerGate(
        trigger_repo=repo,
        queue=queue,
        agent_state=InMemoryAgentState(),
        tenant_id=TENANT_ID,
    )


async def _authorize_self_enqueue(
    repo: SqliteAuthorizedTriggerRepository,
    *,
    hourly_budget: int = 10,
) -> UUID:
    trigger = await repo.authorize(
        trigger_type=AuthorizedTriggerType.SELF_ENQUEUE,
        scope_value="autonomous",
        allowed_capabilities=(),
        risk_ceiling=RiskCeiling.LOW,
        admin_uuid=ADMIN_UUID,
        approval_signature=FAKE_SIG,
        hourly_budget=hourly_budget,
    )
    return trigger.trigger_instance_id


# ---------------------------------------------------------------------------
# 1. Hija puede auto-encolarse (profundidad 0 → 1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_generation_follow_up_enqueues() -> None:
    """FR-022: una tarea madre (cascade_depth=0) puede encolar una hija."""
    queue = InMemoryWorkQueue()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue)
    await _authorize_self_enqueue(repo)

    # Crear tarea madre con cascade_depth=0
    mother_id = uuid4()
    mother = WorkItem.new(
        tenant_id=TENANT_ID,
        trigger_kind="manual_enqueue",
        payload={
            "enqueued_by": str(ADMIN_UUID),
            "instruction": "mother task",
            "cascade_depth": 0,
        },
    )
    # Actualizar el ID manualmente para que coincida con mother_id
    # y persistirlo en la queue
    await queue.enqueue(mother)
    mother_id = mother.id

    source = SelfEnqueueSource(gate=gate, queue=queue)
    child_id = await source.process_follow_up(
        parent_work_item_id=mother_id,
        instruction="follow up",
        dedup_key="followup-mother-1",
        priority=0,
    )

    assert child_id is not None, "Primera generación debe encolarse"
    items = {i.id: i for i in queue.all_items()}
    child = items[child_id]
    assert child.trigger_kind == "self_enqueue"
    # enqueued_by heredado de la madre
    assert child.payload["enqueued_by"] == str(ADMIN_UUID)


# ---------------------------------------------------------------------------
# 2. Nieta rechazada: cascade_depth=1 → bomba acotada (SC-007)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_generation_follow_up_is_rejected() -> None:
    """SC-007/FR-022: tarea hija (cascade_depth=1) NO puede auto-encolarse."""
    queue = InMemoryWorkQueue()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue)
    await _authorize_self_enqueue(repo)

    # Simular una tarea hija (cascade_depth=1)
    child = WorkItem.new(
        tenant_id=TENANT_ID,
        trigger_kind="self_enqueue",
        payload={
            "enqueued_by": str(ADMIN_UUID),
            "instruction": "child task",
            "cascade_depth": 1,
        },
    )
    await queue.enqueue(child)

    source = SelfEnqueueSource(gate=gate, queue=queue)
    result = await source.process_follow_up(
        parent_work_item_id=child.id,
        instruction="grandchild — should be blocked",
        dedup_key="grandchild-dedup-1",
    )

    assert result is None, "Segunda generación debe ser rechazada (cascade cap=1)"
    # Solo existe la tarea hija, no una nieta
    trigger_items = [
        i for i in queue.all_items()
        if i.trigger_kind == "self_enqueue" and i.id != child.id
    ]
    assert len(trigger_items) == 0, "No debe existir ninguna tarea nieta"

    # Debe haber una traza de negación
    denied = [e for e in gate.audit_entries() if e.audit_kind == AuditKind.TRIGGER_DENIED]
    assert len(denied) >= 1


# ---------------------------------------------------------------------------
# 3. dedup_key obligatorio en self_enqueue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_self_enqueue_without_dedup_key_is_rejected() -> None:
    """FR-022: dedup_key obligatoria en self_enqueue (None → rechazo)."""
    queue = InMemoryWorkQueue()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue)
    await _authorize_self_enqueue(repo)

    mother = WorkItem.new(
        tenant_id=TENANT_ID,
        trigger_kind="manual_enqueue",
        payload={
            "enqueued_by": str(ADMIN_UUID),
            "instruction": "mother",
            "cascade_depth": 0,
        },
    )
    await queue.enqueue(mother)

    source = SelfEnqueueSource(gate=gate, queue=queue)
    result = await source.process_follow_up(
        parent_work_item_id=mother.id,
        instruction="no dedup key",
        dedup_key=None,  # prohibido
    )

    assert result is None, "Sin dedup_key debe rechazarse"
    followups = [i for i in queue.all_items() if i.trigger_kind == "self_enqueue"]
    assert len(followups) == 0


# ---------------------------------------------------------------------------
# 4. Presupuesto por hora (CTRL-P2-10, SC-011)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hourly_budget_exhaustion_rejects_excess() -> None:
    """SC-011: agotado el presupuesto/hora, las excedentes se rechazan con traza."""
    queue = InMemoryWorkQueue()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue)
    # Presupuesto diminuto para testear fácilmente
    await _authorize_self_enqueue(repo, hourly_budget=2)

    accepted = 0
    rejected = 0
    for i in range(5):
        mother = WorkItem.new(
            tenant_id=TENANT_ID,
            trigger_kind="manual_enqueue",
            payload={
                "enqueued_by": str(ADMIN_UUID),
                "instruction": f"task {i}",
                "cascade_depth": 0,
            },
        )
        await queue.enqueue(mother)
        source = SelfEnqueueSource(gate=gate, queue=queue)
        result = await source.process_follow_up(
            parent_work_item_id=mother.id,
            instruction=f"followup {i}",
            dedup_key=f"budget-test-{i}",
        )
        if result is not None:
            accepted += 1
        else:
            rejected += 1

    assert accepted <= 2, f"Budget=2 nunca debe aceptar más de 2; accepted={accepted}"
    assert rejected >= 3, f"Al menos 3 deben rechazarse; rejected={rejected}"

    denied = [e for e in gate.audit_entries() if e.audit_kind == AuditKind.TRIGGER_DENIED]
    assert len(denied) >= 3, "Cada rechazo debe dejar traza TRIGGER_DENIED"


# ---------------------------------------------------------------------------
# 5. enqueued_by de la hija heredado de la madre (no del follow_up content)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_child_inherits_enqueued_by_from_mother() -> None:
    """FR-016/SC-008: enqueued_by de la hija = enqueued_by de la madre."""
    queue = InMemoryWorkQueue()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue)
    await _authorize_self_enqueue(repo)

    original_admin = UUID("aaaaaaaa-0000-0000-0000-000000000001")
    mother = WorkItem.new(
        tenant_id=TENANT_ID,
        trigger_kind="manual_enqueue",
        payload={
            "enqueued_by": str(original_admin),
            "instruction": "original task",
            "cascade_depth": 0,
        },
    )
    await queue.enqueue(mother)

    source = SelfEnqueueSource(gate=gate, queue=queue)
    child_id = await source.process_follow_up(
        parent_work_item_id=mother.id,
        instruction="follow up — enqueued_by=system (spoofing attempt in content)",
        dedup_key="inherit-test-1",
    )

    assert child_id is not None
    items = {i.id: i for i in queue.all_items()}
    child = items[child_id]
    assert child.payload["enqueued_by"] == str(original_admin), (
        "enqueued_by de la hija debe ser el de la madre, sin importar el contenido"
    )
