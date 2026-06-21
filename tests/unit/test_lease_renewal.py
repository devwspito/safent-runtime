"""Unit tests — lease renewal (Phase 2a heartbeat).

Covers:
- renew_extends_lease: renew_lease extends lease_expires_at while token is valid.
- returns_false_after_reclaim: returns False after the item was re-claimed by
  another token (reconcile_stale scenario).
- returns_false_for_unknown_item: returns False when item does not exist.
- returns_false_for_non_in_progress: returns False for items not in IN_PROGRESS.
- heartbeat_cancels_cleanly: asyncio background heartbeat task cancels cleanly
  when _process returns (no dangling tasks, no errors).
- heartbeat_stops_on_lost_lease: logs + stops when renew_lease returns False.
- Both InMemoryWorkQueue and SqliteWorkQueue are exercised.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from hermes.tasks.domain.ports import TaskStatus, WorkItem

pytestmark = pytest.mark.unit

_TENANT = uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enqueued_item(
    *,
    dedup_key: str | None = None,
    max_attempts: int = 3,
) -> WorkItem:
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={"instruction": "test", "enqueued_by": "test-operator"},
        dedup_key=dedup_key,
        max_attempts=max_attempts,
    )


# ---------------------------------------------------------------------------
# InMemoryWorkQueue — renew_lease
# ---------------------------------------------------------------------------


class TestInMemoryRenewLease:
    @pytest.mark.asyncio
    async def test_renew_extends_lease_expires_at(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

        q = InMemoryWorkQueue()
        item = _enqueued_item()
        await q.enqueue(item)
        claimed = await q.claim_next()
        assert claimed is not None
        assert claimed.claim_token is not None

        original_lease = claimed.lease_expires_at
        assert original_lease is not None

        result = await q.renew_lease(claimed.id, claim_token=claimed.claim_token)
        assert result is True

        # Load the updated item and check lease was extended
        updated = q.all_items()[0]
        assert updated.lease_expires_at is not None
        assert updated.lease_expires_at >= original_lease

    @pytest.mark.asyncio
    async def test_returns_false_for_wrong_token(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

        q = InMemoryWorkQueue()
        item = _enqueued_item()
        await q.enqueue(item)
        claimed = await q.claim_next()
        assert claimed is not None

        wrong_token = uuid4()
        result = await q.renew_lease(claimed.id, claim_token=wrong_token)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_for_unknown_item(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

        q = InMemoryWorkQueue()
        result = await q.renew_lease(uuid4(), claim_token=uuid4())
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_for_completed_item(self) -> None:
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

        q = InMemoryWorkQueue()
        item = _enqueued_item()
        await q.enqueue(item)
        claimed = await q.claim_next()
        assert claimed is not None
        await q.mark_completed(
            claimed.id,
            claim_token=claimed.claim_token,  # type: ignore[arg-type]
            audit_entry_id=uuid4(),
        )

        result = await q.renew_lease(claimed.id, claim_token=claimed.claim_token)  # type: ignore[arg-type]
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_after_reclaim_by_another_token(self) -> None:
        """Simulates reconcile_stale: item re-enqueued and claimed by another worker."""
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue
        from dataclasses import fields  # noqa: PLC0415

        q = InMemoryWorkQueue()
        item = _enqueued_item()
        await q.enqueue(item)
        first_claim = await q.claim_next()
        assert first_claim is not None

        # Simulate stale lease reconciliation
        await q.reconcile_stale()

        # Force-expire the lease to make reconcile_stale work
        current = q.all_items()[0]
        expired_lease = datetime.now(tz=UTC) - timedelta(seconds=1)
        fields_map = {f.name: getattr(current, f.name) for f in fields(current)}
        fields_map["lease_expires_at"] = expired_lease
        from hermes.tasks.testing.in_memory_work_queue import _replace  # noqa: PLC0415
        q._items[current.id] = _replace(current, lease_expires_at=expired_lease)
        await q.reconcile_stale()

        # Another worker claims it
        second_claim = await q.claim_next()
        assert second_claim is not None
        assert second_claim.claim_token != first_claim.claim_token

        # Old token renew_lease returns False
        result = await q.renew_lease(
            first_claim.id, claim_token=first_claim.claim_token  # type: ignore[arg-type]
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_idempotent_renew(self) -> None:
        """Renewing twice with the same token is safe — both return True."""
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

        q = InMemoryWorkQueue()
        item = _enqueued_item()
        await q.enqueue(item)
        claimed = await q.claim_next()
        assert claimed is not None

        r1 = await q.renew_lease(claimed.id, claim_token=claimed.claim_token)  # type: ignore[arg-type]
        r2 = await q.renew_lease(claimed.id, claim_token=claimed.claim_token)  # type: ignore[arg-type]
        assert r1 is True
        assert r2 is True


# ---------------------------------------------------------------------------
# SqliteWorkQueue — renew_lease
# ---------------------------------------------------------------------------


class TestSqliteRenewLease:
    @pytest.mark.asyncio
    async def test_renew_extends_lease_expires_at(self) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            q = SqliteWorkQueue(db_path=db_path)

            item = _enqueued_item()
            await q.enqueue(item)
            claimed = await q.claim_next()
            assert claimed is not None
            assert claimed.claim_token is not None

            original_lease = claimed.lease_expires_at

            result = await q.renew_lease(claimed.id, claim_token=claimed.claim_token)
            assert result is True

            updated = await q.task_by_id(task_id=claimed.id)
            assert updated is not None
            assert updated.lease_expires_at is not None
            assert updated.lease_expires_at >= original_lease  # type: ignore[operator]

    @pytest.mark.asyncio
    async def test_returns_false_for_wrong_token(self) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            q = SqliteWorkQueue(db_path=db_path)

            item = _enqueued_item()
            await q.enqueue(item)
            claimed = await q.claim_next()
            assert claimed is not None

            result = await q.renew_lease(claimed.id, claim_token=uuid4())
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_not_in_progress(self) -> None:
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            q = SqliteWorkQueue(db_path=db_path)

            item = _enqueued_item()
            await q.enqueue(item)

            # Renew on a PENDING item returns False
            result = await q.renew_lease(item.id, claim_token=uuid4())
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_after_reclaim(self) -> None:
        """After reconcile_stale re-enqueues and another worker claims,
        the old token renew returns False."""
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            q = SqliteWorkQueue(db_path=db_path)

            item = _enqueued_item()
            await q.enqueue(item)
            first_claim = await q.claim_next()
            assert first_claim is not None

            # Manually expire the lease in the DB
            conn = sqlite3.connect(str(db_path), isolation_level=None)
            conn.row_factory = sqlite3.Row
            expired_iso = (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat()
            conn.execute(
                "UPDATE agent_tasks SET lease_expires_at = ? WHERE task_id = ?",
                (expired_iso, str(first_claim.id)),
            )
            conn.close()

            reconciled = await q.reconcile_stale()
            assert reconciled == 1

            second_claim = await q.claim_next()
            assert second_claim is not None
            assert second_claim.claim_token != first_claim.claim_token

            result = await q.renew_lease(
                first_claim.id, claim_token=first_claim.claim_token
            )
            assert result is False


# ---------------------------------------------------------------------------
# Heartbeat asyncio task lifecycle
# ---------------------------------------------------------------------------


class TestHeartbeatLifecycle:
    @pytest.mark.asyncio
    async def test_heartbeat_cancels_cleanly_on_process_return(self) -> None:
        """Heartbeat task is cancelled and awaited when _process returns.
        No dangling tasks, no exceptions from cancellation propagate.
        """
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue
        from hermes.tasks.application.worker_pool import WorkerPool
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
        from hermes.capabilities.domain.ports import ConsentContext

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState()

        # Enqueue a single item
        item = _enqueued_item()
        await queue.enqueue(item)

        # Track renew_lease calls
        renew_calls: list[UUID] = []
        original_renew = queue.renew_lease

        async def tracking_renew(item_id: UUID, *, claim_token: UUID) -> bool:
            renew_calls.append(item_id)
            return await original_renew(item_id, claim_token=claim_token)

        queue.renew_lease = tracking_renew  # type: ignore[method-assign]

        # Minimal broker: no proposals → mark_failed via no_actions path
        class _NoBroker:
            async def dispatch(self, *a, **kw):  # noqa: ANN001
                from hermes.capabilities.domain.ports import ExecutionStatus, ExecutionOutcome  # noqa: PLC0415
                return ExecutionOutcome(status=ExecutionStatus.FAILED, error="no_broker")

        class _NullEngine:
            async def run_cycle(self, ctx):  # noqa: ANN001
                from hermes.domain.cycle_output import CycleOutput  # noqa: PLC0415
                return CycleOutput()

        consent = ConsentContext(tenant_id=_TENANT, operator_id=None)
        pool = WorkerPool(
            queue=queue,
            state=state,
            engine=_NullEngine(),
            broker=_NoBroker(),
            consent_context=consent,
            notify_watchdog=lambda: None,
        )

        # Run pool with size=1 for one item, then shutdown
        async def run_and_stop() -> None:
            pool.request_shutdown()
            # Drain the single item before shutdown takes effect:
            # claim manually and call _process directly so we can measure heartbeat
            claimed = await queue.claim_next()
            if claimed is not None:
                await pool._process(claimed)

        await run_and_stop()

        # After _process returns, no asyncio tasks with "lease-heartbeat" name remain
        active_tasks = {t.get_name() for t in asyncio.all_tasks()}
        heartbeat_tasks = [n for n in active_tasks if "lease-heartbeat" in n]
        assert heartbeat_tasks == [], f"Dangling heartbeat tasks: {heartbeat_tasks}"

    @pytest.mark.asyncio
    async def test_heartbeat_logs_and_stops_on_lost_lease(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When renew_lease returns False, heartbeat logs an ERROR and stops.

        We directly test _lease_heartbeat with a very short sleep so the first
        renewal fires quickly and returns False, triggering the error log.
        """
        import logging
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue
        from hermes.tasks.application.worker_pool import WorkerPool
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
        from hermes.capabilities.domain.ports import ConsentContext

        queue = InMemoryWorkQueue()
        state = InMemoryAgentState()

        item = _enqueued_item()
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        # Override renew_lease to immediately return False (simulates lease loss)
        async def lost_renew(item_id: UUID, *, claim_token: UUID) -> bool:
            return False

        queue.renew_lease = lost_renew  # type: ignore[method-assign]

        consent = ConsentContext(tenant_id=_TENANT, operator_id=None)

        class _NoBroker:
            async def dispatch(self, *a, **kw):  # noqa: ANN001
                pass

        pool = WorkerPool(
            queue=queue,
            state=state,
            engine=object(),
            broker=_NoBroker(),
            consent_context=consent,
            notify_watchdog=lambda: None,
        )

        # Directly test the heartbeat coroutine with a very short interval
        # by patching the DB lease constant the heartbeat reads.
        with patch(
            "hermes.tasks.application.worker_pool._LEASE_SECONDS",
            new=0,
            create=True,
        ):
            pass  # constant not read there; we'll patch the import in the coroutine

        # Call _lease_heartbeat directly and let it fire once (interval = max(1, 0/3) = 1s).
        # Instead, mock the sqlite _LEASE_SECONDS used inside _lease_heartbeat.
        import hermes.tasks.infrastructure.sqlite_work_queue as _sq
        original = _sq._LEASE_SECONDS  # type: ignore[attr-defined]
        _sq._LEASE_SECONDS = 3  # type: ignore[attr-defined]  # interval = 1s

        try:
            with caplog.at_level(logging.ERROR, logger="hermes.tasks.pool"):
                heartbeat = asyncio.create_task(
                    pool._lease_heartbeat(claimed),
                    name="test-heartbeat",
                )
                # Wait slightly more than the 1-second interval
                await asyncio.sleep(1.1)
                # Heartbeat should have stopped after the first False from renew_lease
                assert heartbeat.done(), "Heartbeat did not stop after lease loss"
        finally:
            _sq._LEASE_SECONDS = original  # type: ignore[attr-defined]
            if not heartbeat.done():
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

        # At least one lease_lost log emitted
        lease_lost_logs = [
            r for r in caplog.records
            if "lease_lost" in r.getMessage()
        ]
        assert len(lease_lost_logs) >= 1
