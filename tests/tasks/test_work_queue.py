"""Tests SqliteWorkQueue — deben FALLAR antes de T022.

Cubre:
- enqueue: rechaza sin enqueued_by (CTRL-10); dedup idempotente (SC-007/I5).
- claim_next: atómico, sin doble-toma (FR-003).
- mark_completed: exige audit_entry_id (SC-001/I1); falla sin él.
- mark_failed: backoff + tope max_retries (FR-006/CTRL-16).
- mark_pending_approval: libera lease (LOOP-4).
- reconcile_stale: re-encola huérfanos (SC-003).
- guard claim_token en transiciones (I7).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hermes.tasks.domain.ports import TaskStatus, WorkItem

pytestmark = pytest.mark.unit

_TENANT = uuid4()


def _item(
    *,
    enqueued_by: str | None = "operator-123",
    dedup_key: str | None = None,
    priority: int = 0,
    max_attempts: int = 3,
) -> WorkItem:
    item = WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={"instruction": "do something"},
        dedup_key=dedup_key,
        priority=priority,
        max_attempts=max_attempts,
    )
    if enqueued_by is not None:
        from dataclasses import fields  # noqa: PLC0415
        current = {f.name: getattr(item, f.name) for f in fields(item)}
        current["payload"] = {**current["payload"], "enqueued_by": enqueued_by}
        item = WorkItem(**current)  # type: ignore[arg-type]
    return item


# ---------------------------------------------------------------------------
# In-memory queue tests (unit — no SQLite)
# ---------------------------------------------------------------------------


class TestInMemoryEnqueue:
    async def test_enqueue_returns_item(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        item = _item()
        result = await q.enqueue(item)
        assert result.id == item.id
        assert result.status is TaskStatus.PENDING

    async def test_dedup_idempotent_returns_existing(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        item = _item(dedup_key="job-1")
        first = await q.enqueue(item)
        second_item = _item(dedup_key="job-1")
        second = await q.enqueue(second_item)
        assert second.id == first.id  # devuelve el existente, no el nuevo

    async def test_dedup_allows_terminal_reuse(self) -> None:
        """Una dedup_key en item terminal permite encolar uno nuevo."""
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        item = _item(dedup_key="job-2")
        await q.enqueue(item)
        # Simular completado del item
        claimed = await q.claim_next()
        assert claimed is not None
        audit_id = uuid4()
        await q.mark_completed(claimed.id, claim_token=claimed.claim_token, audit_entry_id=audit_id)
        # Ahora debe poder encolar con la misma dedup_key
        new_item = _item(dedup_key="job-2")
        result = await q.enqueue(new_item)
        assert result.id == new_item.id


class TestInMemoryClaimNext:
    async def test_claim_returns_none_when_empty(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        result = await q.claim_next()
        assert result is None

    async def test_claim_marks_in_progress(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        item = _item()
        await q.enqueue(item)
        claimed = await q.claim_next()
        assert claimed is not None
        assert claimed.status is TaskStatus.IN_PROGRESS

    async def test_claim_sets_claim_token_and_lease(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        await q.enqueue(_item())
        claimed = await q.claim_next()
        assert claimed is not None
        assert claimed.claim_token is not None
        assert claimed.lease_expires_at is not None
        assert claimed.lease_expires_at > datetime.now(tz=UTC)

    async def test_claim_increments_attempts(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        item = _item()
        await q.enqueue(item)
        claimed = await q.claim_next()
        assert claimed is not None
        assert claimed.attempts == item.attempts + 1

    async def test_no_double_claim(self) -> None:
        """Un item IN_PROGRESS no puede ser reclamado de nuevo."""
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        await q.enqueue(_item())
        first = await q.claim_next()
        assert first is not None
        second = await q.claim_next()
        assert second is None  # no hay más items PENDING

    async def test_claim_respects_priority(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        low = _item(priority=0)
        high = _item(priority=10)
        await q.enqueue(low)
        await q.enqueue(high)
        claimed = await q.claim_next()
        assert claimed is not None
        assert claimed.id == high.id

    async def test_claim_skips_items_not_yet_available(self) -> None:
        from dataclasses import fields  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        base_item = WorkItem.new(
            tenant_id=_TENANT,
            trigger_kind="manual_enqueue",
            payload={},
        )
        # Patch available_at to be in the future
        future_item = WorkItem(
            **{f.name: getattr(base_item, f.name) for f in fields(base_item)}
            | {"available_at": datetime.now(tz=UTC) + timedelta(hours=1)}
        )
        await q.enqueue(future_item)
        result = await q.claim_next()
        assert result is None


class TestInMemoryMarkCompleted:
    async def test_mark_completed_requires_audit_entry_id(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        await q.enqueue(_item())
        claimed = await q.claim_next()
        assert claimed is not None
        # Debe aceptar audit_entry_id válido
        await q.mark_completed(
            claimed.id,
            claim_token=claimed.claim_token,
            audit_entry_id=uuid4(),
        )
        items = q.items_with_status(TaskStatus.COMPLETED)
        assert len(items) == 1

    async def test_wrong_claim_token_raises(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        await q.enqueue(_item())
        claimed = await q.claim_next()
        assert claimed is not None
        with pytest.raises((ValueError, Exception)):
            await q.mark_completed(
                claimed.id,
                claim_token=uuid4(),  # token incorrecto
                audit_entry_id=uuid4(),
            )


class TestInMemoryMarkFailed:
    async def test_failed_retryable_reschedules_to_pending(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        await q.enqueue(_item(max_attempts=3))
        claimed = await q.claim_next()
        assert claimed is not None
        result = await q.mark_failed(
            claimed.id, claim_token=claimed.claim_token, reason="transient"
        )
        assert result.status is TaskStatus.PENDING
        assert result.available_at > datetime.now(tz=UTC)

    async def test_failed_terminal_when_exhausted(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        item = _item(max_attempts=1)
        await q.enqueue(item)
        claimed = await q.claim_next()
        assert claimed is not None
        result = await q.mark_failed(
            claimed.id, claim_token=claimed.claim_token, reason="exhausted"
        )
        assert result.status is TaskStatus.FAILED

    async def test_failed_clears_lease(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        item = _item(max_attempts=1)
        await q.enqueue(item)
        claimed = await q.claim_next()
        assert claimed is not None
        result = await q.mark_failed(
            claimed.id, claim_token=claimed.claim_token, reason="err"
        )
        assert result.claim_token is None
        assert result.lease_expires_at is None


class TestInMemoryMarkPendingApproval:
    async def test_pending_approval_releases_lease(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        await q.enqueue(_item())
        claimed = await q.claim_next()
        assert claimed is not None
        await q.mark_pending_approval(
            claimed.id,
            claim_token=claimed.claim_token,
            proposal_id=uuid4(),
        )
        items = q.items_with_status(TaskStatus.PENDING_APPROVAL)
        assert len(items) == 1
        pa = items[0]
        assert pa.claim_token is None
        assert pa.lease_expires_at is None

    async def test_loop_not_blocked_by_pending_approval(self) -> None:
        """Otro item puede ser reclamado después de un PENDING_APPROVAL (LOOP-4)."""
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        item1 = _item()
        item2 = _item()
        await q.enqueue(item1)
        await q.enqueue(item2)

        first = await q.claim_next()
        assert first is not None
        await q.mark_pending_approval(
            first.id, claim_token=first.claim_token, proposal_id=uuid4()
        )

        second = await q.claim_next()
        assert second is not None
        assert second.id == item2.id


class TestInMemoryReconcileStale:
    async def test_reconcile_returns_count(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        count = await q.reconcile_stale()
        assert count == 0

    async def test_reconcile_re_enqueues_expired_lease(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        from dataclasses import fields  # noqa: PLC0415

        q = InMemoryWorkQueue()
        item = _item()
        await q.enqueue(item)
        claimed = await q.claim_next()
        assert claimed is not None

        # Forzar lease vencido directamente en el dict interno
        expired_claimed = WorkItem(
            **{
                f.name: getattr(claimed, f.name)
                for f in fields(claimed)
            }
            | {"lease_expires_at": datetime.now(tz=UTC) - timedelta(seconds=1)}
        )
        q._items[claimed.id] = expired_claimed

        count = await q.reconcile_stale()
        assert count == 1

        items = q.items_with_status(TaskStatus.PENDING)
        assert len(items) == 1

    async def test_reconcile_does_not_touch_active_lease(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        q = InMemoryWorkQueue()
        await q.enqueue(_item())
        await q.claim_next()
        count = await q.reconcile_stale()
        assert count == 0


# ---------------------------------------------------------------------------
# SQLite integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test-tasks.db"


@pytest.mark.integration
class TestSqliteWorkQueueEnqueue:
    async def test_enqueue_inserts_pending(self, db_path: Path) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        item = _item(enqueued_by="op-1")
        result = await q.enqueue(item)
        assert result.id == item.id
        assert result.status is TaskStatus.PENDING

    async def test_enqueue_timer_persists_and_is_claimable(self, db_path: Path) -> None:
        """Regresión 2026-06-28: un item 'timer' con trigger_instance_id en payload
        DEBE persistir y ser reclamable. Antes, enqueue no espejaba la columna
        trigger_instance_id → el CHECK trigger_kind↔trigger_instance_id rechazaba la
        fila y el INSERT OR IGNORE la descartaba EN SILENCIO: la tarea programada
        disparaba pero nunca se encolaba ni ejecutaba.
        """
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        item = WorkItem.new(
            tenant_id=_TENANT,
            trigger_kind="timer",
            payload={
                "enqueued_by": "00000000-0000-0000-0000-0000000003e8",
                "instruction": "escribe proof.txt",
                "trigger_instance_id": str(uuid4()),
            },
            dedup_key="timer-x-2026-06-28T18:00:00+00:00",
        )
        result = await q.enqueue(item)
        assert result.id == item.id
        claimed = await q.claim_next()
        assert claimed is not None
        assert claimed.id == item.id  # realmente quedó en la cola, no se perdió

    async def test_enqueue_timer_without_instance_id_fails_loud(self, db_path: Path) -> None:
        """Fail-loud: un item de trigger que viola el CHECK ya no se traga en
        silencio — eleva WorkQueueIntegrityError en vez de fingir que se encoló."""
        from hermes.tasks.infrastructure.sqlite_work_queue import (  # noqa: PLC0415
            SqliteWorkQueue,
            WorkQueueIntegrityError,
        )
        q = SqliteWorkQueue(db_path=db_path)
        item = WorkItem.new(
            tenant_id=_TENANT,
            trigger_kind="timer",
            payload={"enqueued_by": "00000000-0000-0000-0000-0000000003e8"},
            dedup_key="timer-no-instance",
        )
        with pytest.raises(WorkQueueIntegrityError):
            await q.enqueue(item)

    async def test_enqueue_rejects_without_enqueued_by(self, db_path: Path) -> None:
        """CTRL-10: enqueue rechaza items sin enqueued_by en payload."""
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue, MissingEnqueuedBy  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        item = _item(enqueued_by=None)
        with pytest.raises(MissingEnqueuedBy):
            await q.enqueue(item)

    async def test_enqueue_dedup_idempotent(self, db_path: Path) -> None:
        """SC-007/I5: dedup_key viva devuelve item existente."""
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        item = _item(dedup_key="job-dup-1")
        first = await q.enqueue(item)
        second_item = _item(dedup_key="job-dup-1")
        second = await q.enqueue(second_item)
        assert second.id == first.id

    async def test_enqueue_dedup_allows_terminal_reuse(self, db_path: Path) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        item = _item(dedup_key="job-dup-2")
        await q.enqueue(item)
        claimed = await q.claim_next()
        assert claimed is not None
        await q.mark_completed(claimed.id, claim_token=claimed.claim_token, audit_entry_id=uuid4())
        new_item = _item(dedup_key="job-dup-2")
        result = await q.enqueue(new_item)
        assert result.id == new_item.id


@pytest.mark.integration
class TestSqliteWorkQueueClaimNext:
    async def test_claim_returns_none_when_empty(self, db_path: Path) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        assert await q.claim_next() is None

    async def test_claim_marks_in_progress(self, db_path: Path) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        await q.enqueue(_item())
        claimed = await q.claim_next()
        assert claimed is not None
        assert claimed.status is TaskStatus.IN_PROGRESS

    async def test_no_double_claim(self, db_path: Path) -> None:
        """FR-003: un mismo item no puede ser tomado dos veces."""
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        await q.enqueue(_item())
        first = await q.claim_next()
        assert first is not None
        second = await q.claim_next()
        assert second is None


@pytest.mark.integration
class TestSqliteWorkQueueMarkCompleted:
    async def test_mark_completed_ok(self, db_path: Path) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        await q.enqueue(_item())
        claimed = await q.claim_next()
        assert claimed is not None
        audit_id = uuid4()
        await q.mark_completed(
            claimed.id, claim_token=claimed.claim_token, audit_entry_id=audit_id
        )
        # Verificar que no se puede reclamar de nuevo
        assert await q.claim_next() is None

    async def test_mark_completed_wrong_token_raises(self, db_path: Path) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        await q.enqueue(_item())
        claimed = await q.claim_next()
        assert claimed is not None
        with pytest.raises((ValueError, Exception)):
            await q.mark_completed(
                claimed.id, claim_token=uuid4(), audit_entry_id=uuid4()
            )


@pytest.mark.integration
class TestSqliteCheckI1:
    """I1: completed imposible sin evidencia real (CTRL-9, anti-éxito-alucinado)."""

    async def test_i1_check_fires_on_direct_update(self, db_path: Path) -> None:
        """UPDATE directo a completed sin evidencia => IntegrityError (CHECK I1)."""
        from hermes.tasks.infrastructure.schema import ensure_tasks_schema  # noqa: PLC0415
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        ensure_tasks_schema(conn)
        now_iso = datetime.now(tz=UTC).isoformat()
        future_iso = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
        task_id = str(uuid4())
        claim_tok = str(uuid4())
        # Insertar in_progress VÁLIDO (satisface I3: claim_token/claimed_at/lease; I6: worker_id)
        conn.execute(
            "INSERT INTO agent_tasks "
            "(task_id, trigger_kind, enqueued_by, operator_id, instruction, "
            "status, worker_id, claim_token, claimed_at, lease_expires_at, created_at, updated_at) "
            "VALUES (?, 'manual_enqueue', 'op', 'op', 'do', "
            "'in_progress', 'worker-0', ?, ?, ?, ?, ?)",
            (task_id, claim_tok, now_iso, future_iso, now_iso, now_iso),
        )
        # Intentar completar sin evidencia — debe fallar el CHECK I1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE agent_tasks SET status='completed', "
                "claim_token=NULL, lease_expires_at=NULL "
                "WHERE task_id=?",
                (task_id,),
            )
        conn.close()


@pytest.mark.integration
class TestSqliteWorkQueueMarkFailed:
    async def test_backoff_reschedules_to_pending(self, db_path: Path) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        await q.enqueue(_item(max_attempts=3))
        claimed = await q.claim_next()
        assert claimed is not None
        result = await q.mark_failed(
            claimed.id, claim_token=claimed.claim_token, reason="transient"
        )
        assert result.status is TaskStatus.PENDING
        assert result.available_at > datetime.now(tz=UTC)

    async def test_max_retries_terminal(self, db_path: Path) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        await q.enqueue(_item(max_attempts=1))
        claimed = await q.claim_next()
        assert claimed is not None
        result = await q.mark_failed(
            claimed.id, claim_token=claimed.claim_token, reason="exhausted"
        )
        assert result.status is TaskStatus.FAILED


@pytest.mark.integration
class TestSqliteWorkQueuePendingApproval:
    async def test_pending_approval_releases_lease(self, db_path: Path) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        await q.enqueue(_item())
        claimed = await q.claim_next()
        assert claimed is not None
        await q.mark_pending_approval(
            claimed.id, claim_token=claimed.claim_token, proposal_id=uuid4()
        )

    async def test_loop_not_blocked(self, db_path: Path) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
        q = SqliteWorkQueue(db_path=db_path)
        item1 = _item()
        item2 = _item()
        await q.enqueue(item1)
        await q.enqueue(item2)
        first = await q.claim_next()
        assert first is not None
        await q.mark_pending_approval(
            first.id, claim_token=first.claim_token, proposal_id=uuid4()
        )
        second = await q.claim_next()
        assert second is not None


@pytest.mark.integration
class TestSqliteWorkQueueReconcileStale:
    async def test_reconcile_re_enqueues_expired(self, db_path: Path) -> None:
        from hermes.tasks.infrastructure.schema import ensure_tasks_schema  # noqa: PLC0415
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415

        q = SqliteWorkQueue(db_path=db_path)
        task_id = str(uuid4())
        now_iso = datetime.now(tz=UTC).isoformat()
        expired_iso = (datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat()

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        ensure_tasks_schema(conn)
        claim_tok = str(uuid4())
        conn.execute(
            "INSERT INTO agent_tasks "
            "(task_id, trigger_kind, enqueued_by, operator_id, instruction, "
            "status, worker_id, claim_token, claimed_at, lease_expires_at, created_at, updated_at) "
            "VALUES (?, 'manual_enqueue', 'op', 'op', 'do', "
            "'in_progress', 'worker-0', ?, ?, ?, ?, ?)",
            (task_id, claim_tok, now_iso, expired_iso, now_iso, now_iso),
        )
        conn.close()

        count = await q.reconcile_stale()
        assert count == 1

        claimed = await q.claim_next()
        assert claimed is not None
        assert str(claimed.id) == task_id
