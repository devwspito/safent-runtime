"""Tests FIRST — Default-deny gate (US2, SC-001, CTRL-P2-7).

Con la allow-list VACÍA, NINGUNA fuente automática puede encolar.
Cada intento deja una traza TRIGGER_DENIED.

Verifica:
  - 0 tareas aceptadas de las 3 fuentes con allow-list vacía (SC-001)
  - 100% de los intentos producen audit TRIGGER_DENIED
  - Autorizar un origen → ese origen encola (1 tarea, audit TRIGGER_ACTIVATED)
  - Revocar el origen → vuelve a 0 tareas aceptadas (SC-009)
  - Dos orígenes coexistiendo: revocar uno no afecta al otro (SC-009)
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
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
from hermes.agents_os.application.audit_hash_chain import AuditKind


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

ADMIN_UUID = UUID("00000000-0000-0000-0000-000000000001")
TENANT_ID = UUID("00000000-0000-0000-0000-000000000099")
FAKE_SIG = "sha256:fake-signature-for-tests"


def _make_gate(
    repo: SqliteAuthorizedTriggerRepository,
    queue: InMemoryWorkQueue,
    state: InMemoryAgentState,
) -> TriggerGate:
    return TriggerGate(
        trigger_repo=repo,
        queue=queue,
        agent_state=state,
        tenant_id=TENANT_ID,
    )


# ---------------------------------------------------------------------------
# 1. Default-deny: allow-list vacía → 0 tareas aceptadas, 100% TRIGGER_DENIED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timer_denied_when_allowlist_empty() -> None:
    """FR-012/FR-015/SC-001: con allow-list vacía, timer → TRIGGER_DENIED."""
    queue = InMemoryWorkQueue()
    state = InMemoryAgentState()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue, state)

    result = await gate.enqueue_from_trigger(
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value="*/5 * * * *",
        instruction="check status",
    )

    assert result is None, "Allow-list vacía debe rechazar (fail-closed)"
    assert len(queue.all_items()) == 0, "0 tareas deben existir en la cola"
    denied = [
        e for e in gate.audit_entries()
        if e.audit_kind == AuditKind.TRIGGER_DENIED
    ]
    assert len(denied) == 1, "Debe quedar exactamente 1 traza TRIGGER_DENIED"


@pytest.mark.asyncio
async def test_system_event_denied_when_allowlist_empty() -> None:
    """FR-012/SC-001: system_event → TRIGGER_DENIED con allow-list vacía."""
    queue = InMemoryWorkQueue()
    state = InMemoryAgentState()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue, state)

    result = await gate.enqueue_from_trigger(
        trigger_type=AuthorizedTriggerType.SYSTEM_EVENT,
        scope_value="service_failed",
        instruction="service down",
        derived_from_untrusted_content=True,
    )

    assert result is None
    assert len(queue.all_items()) == 0
    denied = [e for e in gate.audit_entries() if e.audit_kind == AuditKind.TRIGGER_DENIED]
    assert len(denied) == 1


@pytest.mark.asyncio
async def test_self_enqueue_denied_when_allowlist_empty() -> None:
    """FR-012/SC-001: self_enqueue → TRIGGER_DENIED con allow-list vacía."""
    queue = InMemoryWorkQueue()
    state = InMemoryAgentState()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue, state)

    parent_id = uuid4()
    result = await gate.enqueue_from_trigger(
        trigger_type=AuthorizedTriggerType.SELF_ENQUEUE,
        scope_value="autonomous",
        instruction="follow up task",
        dedup_key="followup-abc",
        parent_work_item_id=parent_id,
    )

    assert result is None
    assert len(queue.all_items()) == 0
    denied = [e for e in gate.audit_entries() if e.audit_kind == AuditKind.TRIGGER_DENIED]
    assert len(denied) == 1


@pytest.mark.asyncio
async def test_all_three_sources_denied_produces_three_audit_entries() -> None:
    """SC-001: los 3 tipos de trigger → 3 TRIGGER_DENIED, 0 tareas."""
    queue = InMemoryWorkQueue()
    state = InMemoryAgentState()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue, state)

    parent_id = uuid4()
    results = [
        await gate.enqueue_from_trigger(
            trigger_type=AuthorizedTriggerType.TIMER,
            scope_value="0 * * * *",
            instruction="hourly check",
        ),
        await gate.enqueue_from_trigger(
            trigger_type=AuthorizedTriggerType.SYSTEM_EVENT,
            scope_value="battery_low",
            instruction="battery alert",
            derived_from_untrusted_content=True,
        ),
        await gate.enqueue_from_trigger(
            trigger_type=AuthorizedTriggerType.SELF_ENQUEUE,
            scope_value="autonomous",
            instruction="follow up",
            dedup_key="followup-xyz",
            parent_work_item_id=parent_id,
        ),
    ]

    assert all(r is None for r in results), "Todos deben ser rechazados"
    assert len(queue.all_items()) == 0
    denied = [e for e in gate.audit_entries() if e.audit_kind == AuditKind.TRIGGER_DENIED]
    assert len(denied) == 3


# ---------------------------------------------------------------------------
# 2. Autorizar un origen → encola, audit TRIGGER_ACTIVATED, enqueued_by correcto
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_authorized_timer_enqueues_task() -> None:
    """FR-015/SC-003: origen autorizado → 1 tarea con enqueued_by = admin."""
    queue = InMemoryWorkQueue()
    state = InMemoryAgentState()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue, state)

    await repo.authorize(
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value="0 * * * *",
        allowed_capabilities=(),
        risk_ceiling=RiskCeiling.LOW,
        admin_uuid=ADMIN_UUID,
        approval_signature=FAKE_SIG,
    )

    task_id = await gate.enqueue_from_trigger(
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value="0 * * * *",
        instruction="hourly check",
    )

    assert task_id is not None, "Origen autorizado debe encolar"
    items = queue.all_items()
    assert len(items) == 1
    item = items[0]
    assert item.id == task_id
    # FR-016: enqueued_by = admin que autorizó, NUNCA NULL/'system'
    enqueued_by = item.payload.get("enqueued_by", "")
    assert enqueued_by == str(ADMIN_UUID), (
        f"enqueued_by debe ser el UUID del admin autorizador; got={enqueued_by!r}"
    )

    activated = [e for e in gate.audit_entries() if e.audit_kind == AuditKind.TRIGGER_ACTIVATED]
    assert len(activated) == 1


@pytest.mark.asyncio
async def test_enqueued_by_is_never_null_or_system() -> None:
    """FR-016/CWE-862/SC-003: enqueued_by ≠ None y ≠ 'system'."""
    queue = InMemoryWorkQueue()
    state = InMemoryAgentState()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue, state)

    await repo.authorize(
        trigger_type=AuthorizedTriggerType.SYSTEM_EVENT,
        scope_value="service_failed",
        allowed_capabilities=(),
        risk_ceiling=RiskCeiling.LOW,
        admin_uuid=ADMIN_UUID,
        approval_signature=FAKE_SIG,
    )

    task_id = await gate.enqueue_from_trigger(
        trigger_type=AuthorizedTriggerType.SYSTEM_EVENT,
        scope_value="service_failed",
        instruction="service recovery",
        derived_from_untrusted_content=True,
    )

    assert task_id is not None
    items = queue.all_items()
    assert len(items) == 1
    enqueued_by = items[0].payload.get("enqueued_by", "")
    assert enqueued_by, "enqueued_by no debe ser vacío/None"
    assert enqueued_by != "system", "enqueued_by nunca debe ser 'system'"
    # debe parseable como UUID
    parsed = UUID(enqueued_by)
    assert parsed == ADMIN_UUID


# ---------------------------------------------------------------------------
# 3. Revocar el origen → vuelve a 0 tareas aceptadas inmediatamente (SC-009)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_revoke_stops_enqueuing_immediately() -> None:
    """FR-018/SC-009: revocar origen → encolado posterior rechazado."""
    queue = InMemoryWorkQueue()
    state = InMemoryAgentState()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue, state)

    trigger = await repo.authorize(
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value="0 9 * * *",
        allowed_capabilities=(),
        risk_ceiling=RiskCeiling.LOW,
        admin_uuid=ADMIN_UUID,
        approval_signature=FAKE_SIG,
    )

    # Antes de revocar: funciona
    task_id = await gate.enqueue_from_trigger(
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value="0 9 * * *",
        instruction="morning check",
    )
    assert task_id is not None

    # Revocar
    await repo.revoke(
        trigger_instance_id=trigger.trigger_instance_id,
        admin_uuid=ADMIN_UUID,
    )

    # Después de revocar: rechazado
    result_after = await gate.enqueue_from_trigger(
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value="0 9 * * *",
        instruction="morning check attempt 2",
        dedup_key="revoke-test-2",
    )
    assert result_after is None, "Origen revocado no debe encolar"

    denied_after = [
        e for e in gate.audit_entries()
        if e.audit_kind == AuditKind.TRIGGER_DENIED
    ]
    assert len(denied_after) >= 1, "Debe quedar traza TRIGGER_DENIED tras revocación"


@pytest.mark.asyncio
async def test_revoke_one_does_not_affect_other_origin(  # SC-009
) -> None:
    """SC-009: revocar un origen no afecta a un segundo origen autorizado."""
    queue = InMemoryWorkQueue()
    state = InMemoryAgentState()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue, state)

    trigger_a = await repo.authorize(
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value="0 8 * * *",
        allowed_capabilities=(),
        risk_ceiling=RiskCeiling.LOW,
        admin_uuid=ADMIN_UUID,
        approval_signature=FAKE_SIG,
    )
    await repo.authorize(
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value="0 12 * * *",
        allowed_capabilities=(),
        risk_ceiling=RiskCeiling.LOW,
        admin_uuid=ADMIN_UUID,
        approval_signature=FAKE_SIG,
    )

    # Revocar solo trigger_a
    await repo.revoke(
        trigger_instance_id=trigger_a.trigger_instance_id,
        admin_uuid=ADMIN_UUID,
    )

    # trigger_a rechazado
    res_a = await gate.enqueue_from_trigger(
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value="0 8 * * *",
        instruction="should be denied",
    )
    assert res_a is None

    # trigger_b todavía funciona
    res_b = await gate.enqueue_from_trigger(
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value="0 12 * * *",
        instruction="noon check",
    )
    assert res_b is not None, "El segundo origen (no revocado) debe seguir funcionando"


# ---------------------------------------------------------------------------
# 4. Tareas auto-disparadas usan trigger_kind correcto (whitelist positiva)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_task_trigger_kind_is_whitelist_value() -> None:
    """FR-019: trigger_kind de tarea auto es el tipo del AuthorizedTriggerType."""
    queue = InMemoryWorkQueue()
    state = InMemoryAgentState()
    repo = SqliteAuthorizedTriggerRepository.in_memory()
    gate = _make_gate(repo, queue, state)

    await repo.authorize(
        trigger_type=AuthorizedTriggerType.SYSTEM_EVENT,
        scope_value="network_changed",
        allowed_capabilities=(),
        risk_ceiling=RiskCeiling.LOW,
        admin_uuid=ADMIN_UUID,
        approval_signature=FAKE_SIG,
    )

    task_id = await gate.enqueue_from_trigger(
        trigger_type=AuthorizedTriggerType.SYSTEM_EVENT,
        scope_value="network_changed",
        instruction="network changed handler",
        derived_from_untrusted_content=True,
    )
    assert task_id is not None
    items = queue.all_items()
    assert items[0].trigger_kind == "system_event"
