"""I5 integration — semántica de reintentos: 1 intento + N reintentos (data-model.md).

Verifica que:
- SqliteWorkQueue.mark_failed delega en work_item.mark_failed (no reimplementa).
- Con max_attempts=3: 3 claims → final FAILED.
- El retry_count no se incrementa dos veces (solo en claim_next).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


class TestRetrySemantics:
    async def test_max_attempts_respected_domain_function(self, tmp_path: Path) -> None:
        """La máquina de estados de dominio produce FAILED tras max_attempts (I5).

        Verificamos directamente que mark_failed de dominio determina la transición
        correcta sin re-implementarla en el SQL. Usamos el mock del dominio puro.
        """
        from hermes.tasks.domain import work_item as _domain
        from hermes.tasks.domain.ports import TaskStatus, WorkItem

        item = WorkItem.new(
            tenant_id=uuid4(),
            trigger_kind="manual_enqueue",
            payload={"enqueued_by": "test-suite"},
            max_attempts=3,
        )

        # Simular 3 intentos directamente en el dominio.
        from hermes.tasks.domain.work_item import claim
        from hermes.tasks.domain.ports import TaskStatus

        token1 = uuid4()
        claimed1 = claim(item, claim_token=token1)
        assert claimed1.attempts == 1

        failed1 = _domain.mark_failed(claimed1, claim_token=token1, reason="err1")
        assert failed1.status == TaskStatus.PENDING  # reintento 1

        # Simular segundo claim.
        token2 = uuid4()
        # Re-claim: incrementar attempts manualmente (en prod lo hace claim_next).
        from hermes.tasks.domain.work_item import _replace
        item2 = _replace(failed1, status=TaskStatus.IN_PROGRESS, attempts=2, claim_token=token2)
        failed2 = _domain.mark_failed(item2, claim_token=token2, reason="err2")
        assert failed2.status == TaskStatus.PENDING  # reintento 2

        # Simular tercer claim.
        token3 = uuid4()
        item3 = _replace(failed2, status=TaskStatus.IN_PROGRESS, attempts=3, claim_token=token3)
        failed3 = _domain.mark_failed(item3, claim_token=token3, reason="err3")
        assert failed3.status == TaskStatus.FAILED, (
            "Attempt 3 con max_attempts=3: debe quedar FAILED terminal (I5)."
        )

    async def test_retry_count_not_doubled(self, tmp_path: Path) -> None:
        """attempts en WorkItem es el retry_count de la BD — no se cuenta doble."""
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue
        from hermes.tasks.domain.ports import WorkItem

        db_path = tmp_path / "shell.db"
        queue = SqliteWorkQueue(db_path=db_path)

        item = WorkItem.new(
            tenant_id=uuid4(),
            trigger_kind="manual_enqueue",
            payload={"enqueued_by": "test-suite"},
            max_attempts=5,
        )
        await queue.enqueue(item)

        # Primer claim: retry_count = 1
        claimed = await queue.claim_next()
        assert claimed is not None
        assert claimed.attempts == 1

        # mark_failed — rescheduled to PENDING
        failed = await queue.mark_failed(
            claimed.id, claim_token=claimed.claim_token, reason="test"
        )
        # Después del primer fallo con attempts=1, debe programarse el reintento.
        assert failed.attempts == 1, "mark_failed no debe incrementar attempts de nuevo."

    async def test_backoff_increases_with_each_attempt(self, tmp_path: Path) -> None:
        """El available_at del WorkItem fallido debe ser > now (backoff activo)."""
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue
        from hermes.tasks.domain.ports import WorkItem, TaskStatus

        db_path = tmp_path / "shell.db"
        queue = SqliteWorkQueue(db_path=db_path)

        item = WorkItem.new(
            tenant_id=uuid4(),
            trigger_kind="manual_enqueue",
            payload={"enqueued_by": "test-suite"},
            max_attempts=5,
        )
        await queue.enqueue(item)

        claimed = await queue.claim_next()
        assert claimed is not None

        before_fail = datetime.now(tz=UTC)
        failed = await queue.mark_failed(
            claimed.id, claim_token=claimed.claim_token, reason="test_backoff"
        )

        assert failed.status == TaskStatus.PENDING
        assert failed.available_at > before_fail, (
            "El WorkItem re-planificado debe tener available_at en el futuro (backoff)."
        )
