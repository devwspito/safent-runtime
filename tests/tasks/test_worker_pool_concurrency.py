"""Concurrency regression tests for Phase 1 parallel worker pool.

Covers five invariants that must hold under concurrent execution:

  1. No double-claim: SqliteWorkQueue.claim_next() with BEGIN IMMEDIATE guarantees
     each item is claimed exactly once when N coroutines race simultaneously.

  2. Watchdog independence: _watchdog_loop fires on its own cadence even when all
     workers are stuck inside a long-running _process call.

  3. Lease re-claim semantics: a stale item is reconciled by reconcile_stale() and
     re-claimable by a new worker; the old claim_token can no longer transition the
     item (ClaimTokenMismatch), so a crashed worker cannot corrupt queue state.

  4. _resolve_worker_pool_size() sizing: env override is respected; value is clamped
     to [1, hard_cap]; /proc/meminfo unreadable → safe bounded fallback.

  5. active_worker_count reflects in-flight: counter rises while workers process and
     returns to 0 after all items are drained.

Note on duplicate side-effects (Phase 2 lease-renewal):
  Lease expiry + re-claim prevents the queue from being permanently stuck if a
  worker crashes mid-processing, but it does NOT prevent the side-effect of the
  original operation from running to partial completion.  The current guarantee
  is queue-state safety only.  Phase 2 will add per-worker heartbeat/renewal so
  that a healthy long-running worker keeps its lease alive and reconcile_stale()
  only touches genuinely orphaned items.  Until then, set HERMES_TASK_LEASE_SECONDS
  conservatively (default 600 s) — longer than the slowest possible LLM cycle.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from hermes.tasks.domain.ports import TaskStatus, WorkItem

pytestmark = pytest.mark.unit

_TENANT = uuid4()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _item(*, priority: int = 0, max_attempts: int = 3) -> WorkItem:
    """Build a minimal valid WorkItem with a required enqueued_by payload."""
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={"instruction": "concurrency-test", "enqueued_by": "test-harness"},
        priority=priority,
        max_attempts=max_attempts,
    )


def _sqlite_queue(db_path: Path, *, worker_id: str = "worker-0"):
    from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
    return SqliteWorkQueue(db_path=db_path, worker_id=worker_id)


# ---------------------------------------------------------------------------
# 1. No double-claim under N concurrent claim_next() calls
# ---------------------------------------------------------------------------


class TestNoDoubleClaimConcurrent:
    """BEGIN IMMEDIATE guarantees at-most-once delivery under N concurrent claimers."""

    @pytest.fixture()
    def db_path(self, tmp_path: Path) -> Path:
        return tmp_path / "concurrent-claim.db"

    @pytest.mark.integration
    async def test_m_items_n_workers_each_claimed_exactly_once(
        self, db_path: Path
    ) -> None:
        """Enqueue M items, fire N concurrent claim_next() calls, assert each item
        is claimed at most once and every returned claim_token is unique.

        M=10, N=16 — intentionally MORE claimers than items: the surplus claimers
        must get None (queue drained), never a duplicate. Exposes any race where
        two coroutines slip through the SELECT before the UPDATE IMMEDIATE fires.
        """
        m_items = 10
        n_workers = 16

        queue = _sqlite_queue(db_path)

        items: list[WorkItem] = []
        for _ in range(m_items):
            enqueued = await queue.enqueue(_item())
            items.append(enqueued)

        # Fire all claims simultaneously — asyncio.gather schedules them as a
        # single batch; the event loop interleaves them at every await point.
        claimed_results: list[WorkItem | None] = list(
            await asyncio.gather(*[queue.claim_next() for _ in range(n_workers)])
        )

        claimed = [r for r in claimed_results if r is not None]

        # Every returned item must be distinct (no two workers got the same item).
        claimed_ids = [c.id for c in claimed]
        assert len(claimed_ids) == len(set(claimed_ids)), (
            f"Double-claim detected — same item returned to multiple workers. "
            f"IDs: {claimed_ids}"
        )

        # We have exactly M items and N > M workers; exactly M should be claimed.
        assert len(claimed) == m_items, (
            f"Expected {m_items} claimed items, got {len(claimed)}. "
            "Some items were lost or duplicate-claimed."
        )

        # Every claim_token must be unique across concurrent claimers.
        tokens = [c.claim_token for c in claimed]
        assert len(tokens) == len(set(tokens)), (
            "Two items share the same claim_token — atomicity broken."
        )

    @pytest.mark.integration
    async def test_second_pass_returns_none_when_drained(
        self, db_path: Path
    ) -> None:
        """After all items are claimed, every further claim_next() returns None."""
        n_items = 5

        queue = _sqlite_queue(db_path)
        for _ in range(n_items):
            await queue.enqueue(_item())

        # Drain everything.
        await asyncio.gather(*[queue.claim_next() for _ in range(n_items)])

        # Second pass — queue is empty, all must return None.
        second_pass: list[WorkItem | None] = list(
            await asyncio.gather(*[queue.claim_next() for _ in range(n_items)])
        )
        assert all(r is None for r in second_pass), (
            f"Expected all None on drained queue; got: "
            f"{[r for r in second_pass if r is not None]}"
        )

    @pytest.mark.integration
    async def test_high_contention_eight_workers_single_item(
        self, db_path: Path
    ) -> None:
        """Single item, 8 concurrent claimers — exactly one succeeds."""
        queue = _sqlite_queue(db_path)
        await queue.enqueue(_item())

        results: list[WorkItem | None] = list(
            await asyncio.gather(*[queue.claim_next() for _ in range(8)])
        )

        successes = [r for r in results if r is not None]
        assert len(successes) == 1, (
            f"Expected exactly 1 claimer to succeed; {len(successes)} succeeded."
        )


# ---------------------------------------------------------------------------
# 2. Watchdog non-blocking under load
# ---------------------------------------------------------------------------


class TestWatchdogIndependentUnderLoad:
    """_watchdog_loop fires on its own cadence even when all workers are busy."""

    async def test_watchdog_fires_while_all_workers_blocked(self) -> None:
        """With N workers all stuck behind a barrier, the watchdog still fires.

        Strategy:
          - Set HERMES_WATCHDOG_INTERVAL_S to a very small value (0.02 s) so
            we can observe multiple firings in < 200 ms of wall time.
          - Give each worker a processing function that blocks until released by
            an asyncio.Event (no real sleep — deterministic control).
          - Count watchdog notify calls over the blocking period.
          - Assert notify was called at least twice (proves it is not gated on
            workers finishing).
        """
        from hermes.tasks.application.worker_pool import WorkerPool  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        from hermes.testing import FakeReasoningEngine  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415
        from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415

        notify_calls: list[float] = []
        worker_release = asyncio.Event()

        # Tiny interval so we can observe multiple ticks quickly.
        watchdog_interval = 0.02  # 20 ms

        n_workers = 3
        queue = InMemoryWorkQueue()
        for _ in range(n_workers):
            await queue.enqueue(_item())

        def _counting_notify() -> None:
            import time  # noqa: PLC0415
            notify_calls.append(time.monotonic())

        pool = WorkerPool(
            queue=queue,
            state=InMemoryAgentState(),
            engine=FakeReasoningEngine(),
            broker=FakeCapabilityBroker(),
            consent_context=ConsentContext(tenant_id=_TENANT, operator_id=None),
            notify_watchdog=_counting_notify,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        # Patch _process so workers block until worker_release is set.
        async def _blocking_process(_item_arg) -> None:  # noqa: ANN001
            await worker_release.wait()

        pool._process = _blocking_process  # type: ignore[method-assign]

        # Override watchdog interval on the module constant for this test scope.
        with patch(
            "hermes.tasks.application.worker_pool._WATCHDOG_INTERVAL_S",
            watchdog_interval,
        ):
            # Launch pool in background; workers will block immediately.
            pool_task = asyncio.create_task(pool.run_forever(size=n_workers))

            # Allow the pool to start and workers to enter _blocking_process.
            # We need enough yields for all workers to be dispatched.
            for _ in range(20):
                await asyncio.sleep(0)

            # Observe watchdog ticks for ~5 intervals while workers are blocked.
            observe_duration = watchdog_interval * 5
            await asyncio.sleep(observe_duration)

            ticks_while_blocked = len(notify_calls)

            # Release workers and shut down cleanly.
            worker_release.set()
            pool.request_shutdown()
            await asyncio.wait_for(pool_task, timeout=2.0)

        assert ticks_while_blocked >= 2, (
            f"Watchdog fired only {ticks_while_blocked} times while workers were "
            f"blocked (expected >= 2 in {observe_duration:.3f}s with "
            f"{watchdog_interval}s interval). "
            "Watchdog is NOT independent of worker progress — T067 regression."
        )


# ---------------------------------------------------------------------------
# 3. Lease re-claim semantics + stale token cannot corrupt state
# ---------------------------------------------------------------------------


class TestLeaseReClaimSemantics:
    """reconcile_stale() re-enqueues expired leases; the stale claim_token is refused."""

    @pytest.fixture()
    def db_path(self, tmp_path: Path) -> Path:
        return tmp_path / "lease-test.db"

    @pytest.mark.integration
    async def test_expired_lease_item_re_claimable_after_reconcile(
        self, db_path: Path
    ) -> None:
        """Item with expired lease is returned to PENDING by reconcile_stale() and
        can then be claimed again by a fresh claim_next() call.

        We force the expiry by directly inserting an in_progress row with a
        lease_expires_at in the past via raw SQLite (same technique as the
        existing TestSqliteWorkQueueReconcileStale), bypassing the normal claim
        flow to get a deterministic expired state without sleeping.
        """
        from hermes.tasks.infrastructure.schema import ensure_tasks_schema  # noqa: PLC0415
        from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415

        queue = SqliteWorkQueue(db_path=db_path)
        task_id = str(uuid4())
        stale_token = str(uuid4())
        now_iso = datetime.now(tz=UTC).isoformat()
        expired_iso = (datetime.now(tz=UTC) - timedelta(minutes=15)).isoformat()

        # Insert as in_progress with a lease already expired.
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        ensure_tasks_schema(conn)
        conn.execute(
            "INSERT INTO agent_tasks "
            "(task_id, trigger_kind, enqueued_by, operator_id, instruction, "
            " status, worker_id, claim_token, claimed_at, lease_expires_at, "
            " created_at, updated_at) "
            "VALUES (?, 'manual_enqueue', 'test-harness', 'test-harness', 'do', "
            " 'in_progress', 'worker-stale', ?, ?, ?, ?, ?)",
            (task_id, stale_token, now_iso, expired_iso, now_iso, now_iso),
        )
        conn.close()

        # reconcile_stale() must re-enqueue it.
        reconciled = await queue.reconcile_stale()
        assert reconciled == 1, (
            f"Expected 1 reconciled item; got {reconciled}."
        )

        # A fresh worker can now claim it.
        fresh_item = await queue.claim_next()
        assert fresh_item is not None, (
            "claim_next() returned None after reconcile_stale() — item was not "
            "returned to PENDING."
        )
        assert str(fresh_item.id) == task_id
        assert fresh_item.status is TaskStatus.IN_PROGRESS
        assert fresh_item.claim_token is not None
        assert str(fresh_item.claim_token) != stale_token, (
            "Fresh claim should have a NEW token, not the stale one."
        )

    @pytest.mark.integration
    async def test_stale_claim_token_cannot_mark_completed(
        self, db_path: Path
    ) -> None:
        """The original (stale) claim_token is rejected when attempting mark_completed.

        This is the core safety property: a crashed worker that woke up late cannot
        mark a re-claimed item as completed using its outdated token.

        NOTE (Phase 2): token-guarding prevents queue-state corruption here. However,
        if the crashed worker already began executing its side-effect (e.g. wrote a
        file), that duplicate side-effect is NOT prevented by the queue alone.
        Phase 2 lease-renewal/heartbeat will shrink this window; Phase 2 idempotency
        keys in the domain will eliminate the duplicate-effect risk entirely.
        """
        from hermes.tasks.infrastructure.schema import ensure_tasks_schema  # noqa: PLC0415
        from hermes.tasks.infrastructure.sqlite_work_queue import (  # noqa: PLC0415
            ClaimTokenMismatch,
            SqliteWorkQueue,
        )

        queue = SqliteWorkQueue(db_path=db_path)
        task_id = str(uuid4())
        stale_token_str = str(uuid4())
        stale_token_uuid = UUID(stale_token_str)
        now_iso = datetime.now(tz=UTC).isoformat()
        expired_iso = (datetime.now(tz=UTC) - timedelta(minutes=15)).isoformat()

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        ensure_tasks_schema(conn)
        conn.execute(
            "INSERT INTO agent_tasks "
            "(task_id, trigger_kind, enqueued_by, operator_id, instruction, "
            " status, worker_id, claim_token, claimed_at, lease_expires_at, "
            " created_at, updated_at) "
            "VALUES (?, 'manual_enqueue', 'test-harness', 'test-harness', 'do', "
            " 'in_progress', 'worker-stale', ?, ?, ?, ?, ?)",
            (task_id, stale_token_str, now_iso, expired_iso, now_iso, now_iso),
        )
        conn.close()

        # Reconcile so a new worker can claim it.
        await queue.reconcile_stale()
        fresh_item = await queue.claim_next()
        assert fresh_item is not None

        # Stale worker tries to complete using its old token — must be refused.
        with pytest.raises(ClaimTokenMismatch):
            await queue.mark_completed(
                UUID(task_id),
                claim_token=stale_token_uuid,
                audit_entry_id=uuid4(),
            )

        # Queue state must be unaffected: fresh worker's item still IN_PROGRESS.
        loaded = await queue.task_by_id(task_id=UUID(task_id))
        assert loaded is not None
        assert loaded.status is TaskStatus.IN_PROGRESS, (
            "Stale mark_completed should not have transitioned the item away from "
            "IN_PROGRESS — the fresh worker's claim must remain intact."
        )

    @pytest.mark.integration
    async def test_stale_claim_token_cannot_mark_failed(
        self, db_path: Path
    ) -> None:
        """The stale claim_token is also rejected by mark_failed — same invariant."""
        from hermes.tasks.infrastructure.schema import ensure_tasks_schema  # noqa: PLC0415
        from hermes.tasks.infrastructure.sqlite_work_queue import (  # noqa: PLC0415
            ClaimTokenMismatch,
            SqliteWorkQueue,
        )

        queue = SqliteWorkQueue(db_path=db_path)
        task_id = str(uuid4())
        stale_token_str = str(uuid4())
        stale_token_uuid = UUID(stale_token_str)
        now_iso = datetime.now(tz=UTC).isoformat()
        expired_iso = (datetime.now(tz=UTC) - timedelta(minutes=15)).isoformat()

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        ensure_tasks_schema(conn)
        conn.execute(
            "INSERT INTO agent_tasks "
            "(task_id, trigger_kind, enqueued_by, operator_id, instruction, "
            " status, worker_id, claim_token, claimed_at, lease_expires_at, "
            " created_at, updated_at) "
            "VALUES (?, 'manual_enqueue', 'test-harness', 'test-harness', 'do', "
            " 'in_progress', 'worker-stale', ?, ?, ?, ?, ?)",
            (task_id, stale_token_str, now_iso, expired_iso, now_iso, now_iso),
        )
        conn.close()

        await queue.reconcile_stale()
        fresh_item = await queue.claim_next()
        assert fresh_item is not None

        with pytest.raises(ClaimTokenMismatch):
            await queue.mark_failed(
                UUID(task_id),
                claim_token=stale_token_uuid,
                reason="stale worker giving up",
            )

        loaded = await queue.task_by_id(task_id=UUID(task_id))
        assert loaded is not None
        assert loaded.status is TaskStatus.IN_PROGRESS, (
            "Stale mark_failed must not corrupt the fresh worker's in-progress item."
        )


# ---------------------------------------------------------------------------
# 4. _resolve_worker_pool_size() — env override, clamping, fallback
# ---------------------------------------------------------------------------


class TestResolveWorkerPoolSize:
    """_resolve_worker_pool_size() must be deterministic under all conditions."""

    def test_env_override_respected(self) -> None:
        """HERMES_WORKER_POOL_SIZE env var sets the pool size directly."""
        from hermes.tasks.application.worker_pool import _resolve_worker_pool_size  # noqa: PLC0415

        with patch.dict(os.environ, {"HERMES_WORKER_POOL_SIZE": "7"}, clear=False):
            # Remove hard-cap interference by ensuring 7 <= hard_cap.
            size = _resolve_worker_pool_size()
        assert size == 7, f"Expected 7 from env var, got {size}"

    def test_env_override_clamps_to_minimum_one(self) -> None:
        """HERMES_WORKER_POOL_SIZE=0 is clamped to 1 — pool must have >= 1 worker."""
        from hermes.tasks.application.worker_pool import _resolve_worker_pool_size  # noqa: PLC0415

        with patch.dict(os.environ, {"HERMES_WORKER_POOL_SIZE": "0"}, clear=False):
            size = _resolve_worker_pool_size()
        assert size == 1, f"Expected clamped minimum 1 from env=0, got {size}"

    def test_env_override_clamps_to_hard_cap(self) -> None:
        """HERMES_WORKER_POOL_SIZE > hard_cap is clamped to hard_cap."""
        from hermes.tasks.application.worker_pool import _resolve_worker_pool_size  # noqa: PLC0415

        hard_cap = 4  # deliberately small for the test
        env = {
            "HERMES_WORKER_POOL_SIZE": str(hard_cap + 100),
            "HERMES_WORKER_POOL_HARD_CAP": str(hard_cap),
        }
        with patch.dict(os.environ, env, clear=False):
            size = _resolve_worker_pool_size()
        assert size == hard_cap, (
            f"Expected size clamped to hard_cap={hard_cap}, got {size}"
        )

    def test_env_override_negative_clamps_to_one(self) -> None:
        """Negative HERMES_WORKER_POOL_SIZE is clamped to 1."""
        from hermes.tasks.application.worker_pool import _resolve_worker_pool_size  # noqa: PLC0415

        with patch.dict(os.environ, {"HERMES_WORKER_POOL_SIZE": "-5"}, clear=False):
            size = _resolve_worker_pool_size()
        assert size == 1, f"Expected clamped minimum 1 from env=-5, got {size}"

    def test_meminfo_unreadable_returns_safe_fallback(self) -> None:
        """/proc/meminfo unreadable (non-Linux or permission denied) → safe fallback.

        The fallback (_MEMINFO_FALLBACK_WORKERS=8 by default) must be >= 1 and
        <= hard_cap so it is always a valid pool size.
        """
        from hermes.tasks.application.worker_pool import (  # noqa: PLC0415
            _resolve_worker_pool_size,
            _MEMINFO_FALLBACK_WORKERS,
            _WORKER_POOL_HARD_CAP,
        )

        # Remove env override so the RAM-based path is exercised.
        env_without_size = {k: v for k, v in os.environ.items()
                            if k != "HERMES_WORKER_POOL_SIZE"}

        with (
            patch.dict(os.environ, env_without_size, clear=True),
            patch(
                "hermes.tasks.application.worker_pool._read_mem_available_mb",
                return_value=None,
            ),
        ):
            size = _resolve_worker_pool_size()

        assert size == _MEMINFO_FALLBACK_WORKERS, (
            f"Expected fallback {_MEMINFO_FALLBACK_WORKERS} when /proc/meminfo "
            f"is unreadable, got {size}"
        )
        assert 1 <= size <= _WORKER_POOL_HARD_CAP, (
            f"Fallback {size} is outside valid range [1, {_WORKER_POOL_HARD_CAP}]"
        )

    def test_ram_based_sizing_respects_hard_cap(self) -> None:
        """Even with enormous MemAvailable, size is capped at hard_cap."""
        from hermes.tasks.application.worker_pool import _resolve_worker_pool_size  # noqa: PLC0415

        hard_cap = 3
        env = {"HERMES_WORKER_POOL_HARD_CAP": str(hard_cap)}
        env_without_size = {k: v for k, v in os.environ.items()
                            if k != "HERMES_WORKER_POOL_SIZE"}
        env_without_size.update(env)

        with (
            patch.dict(os.environ, env_without_size, clear=True),
            patch(
                "hermes.tasks.application.worker_pool._read_mem_available_mb",
                return_value=999_999,  # unrealistically large
            ),
        ):
            size = _resolve_worker_pool_size()

        assert size == hard_cap, (
            f"RAM-based size should be capped at {hard_cap}, got {size}"
        )

    def test_ram_based_sizing_minimum_one_when_memory_very_low(self) -> None:
        """With very low MemAvailable (less than one worker overhead), size = 1."""
        from hermes.tasks.application.worker_pool import (  # noqa: PLC0415
            _resolve_worker_pool_size,
            _WORKER_OVERHEAD_MB,
        )

        env_without_size = {k: v for k, v in os.environ.items()
                            if k != "HERMES_WORKER_POOL_SIZE"}

        # Provide less RAM than a single worker overhead.
        with (
            patch.dict(os.environ, env_without_size, clear=True),
            patch(
                "hermes.tasks.application.worker_pool._read_mem_available_mb",
                return_value=max(0, _WORKER_OVERHEAD_MB - 1),
            ),
        ):
            size = _resolve_worker_pool_size()

        assert size == 1, (
            f"Expected minimum 1 worker when RAM < one worker overhead, got {size}"
        )


# ---------------------------------------------------------------------------
# 5. active_worker_count reflects in-flight processing
# ---------------------------------------------------------------------------


class TestActiveWorkerCount:
    """active_worker_count() rises while workers process and returns to 0 when idle."""

    async def test_counter_rises_to_number_of_concurrent_workers(self) -> None:
        """With N items in queue and N workers blocked in _process, the counter
        must reach N while processing is happening, then return to 0 after all
        workers finish.
        """
        from hermes.tasks.application.worker_pool import WorkerPool  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        from hermes.testing import FakeReasoningEngine  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415
        from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415

        n_workers = 4
        worker_blocked = asyncio.Barrier(n_workers)   # wait until all N are in flight
        worker_release = asyncio.Event()

        queue = InMemoryWorkQueue()
        for _ in range(n_workers):
            await queue.enqueue(_item())

        pool = WorkerPool(
            queue=queue,
            state=InMemoryAgentState(),
            engine=FakeReasoningEngine(),
            broker=FakeCapabilityBroker(),
            consent_context=ConsentContext(tenant_id=_TENANT, operator_id=None),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        peak_count_observed: list[int] = []

        async def _controlled_process(_item_arg) -> None:  # noqa: ANN001
            # Signal that this worker is now actively processing.
            await worker_blocked.wait()
            # Capture the counter at the moment all N are in-flight.
            peak_count_observed.append(pool.active_worker_count())
            # Wait until the test releases us.
            await worker_release.wait()

        pool._process = _controlled_process  # type: ignore[method-assign]

        with patch(
            "hermes.tasks.application.worker_pool._WATCHDOG_INTERVAL_S", 1000.0
        ):
            pool_task = asyncio.create_task(pool.run_forever(size=n_workers))

            # Wait for all N workers to be simultaneously active.
            # Poll rather than blocking forever — if the barrier is never reached
            # (e.g. fewer items than workers) this will time out cleanly.
            await asyncio.wait_for(
                _wait_until(lambda: len(peak_count_observed) >= n_workers),
                timeout=2.0,
            )

            assert pool.active_worker_count() == n_workers, (
                f"Expected active_worker_count()={n_workers} while all workers "
                f"are in flight; got {pool.active_worker_count()}"
            )

            # Release all workers.
            worker_release.set()

            # Wait for pool to drain and go idle.
            pool.request_shutdown()
            await asyncio.wait_for(pool_task, timeout=2.0)

        assert pool.active_worker_count() == 0, (
            f"Expected active_worker_count()=0 after pool drains; "
            f"got {pool.active_worker_count()}"
        )

    async def test_counter_is_zero_before_run_forever(self) -> None:
        """A freshly constructed WorkerPool reports 0 active workers."""
        from hermes.tasks.application.worker_pool import WorkerPool  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        from hermes.testing import FakeReasoningEngine  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415
        from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415

        pool = WorkerPool(
            queue=InMemoryWorkQueue(),
            state=InMemoryAgentState(),
            engine=FakeReasoningEngine(),
            broker=FakeCapabilityBroker(),
            consent_context=ConsentContext(tenant_id=_TENANT, operator_id=None),
            notify_watchdog=lambda: None,
        )
        assert pool.active_worker_count() == 0

    async def test_configured_size_none_before_run_forever(self) -> None:
        """configured_size() returns None before run_forever() is called."""
        from hermes.tasks.application.worker_pool import WorkerPool  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        from hermes.testing import FakeReasoningEngine  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415
        from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415

        pool = WorkerPool(
            queue=InMemoryWorkQueue(),
            state=InMemoryAgentState(),
            engine=FakeReasoningEngine(),
            broker=FakeCapabilityBroker(),
            consent_context=ConsentContext(tenant_id=_TENANT, operator_id=None),
            notify_watchdog=lambda: None,
        )
        assert pool.configured_size() is None

    async def test_configured_size_reflects_run_forever_size_arg(self) -> None:
        """configured_size() returns the size passed to run_forever() after it starts."""
        from hermes.tasks.application.worker_pool import WorkerPool  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState  # noqa: PLC0415
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue  # noqa: PLC0415
        from hermes.testing import FakeReasoningEngine  # noqa: PLC0415
        from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker  # noqa: PLC0415
        from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415

        pool = WorkerPool(
            queue=InMemoryWorkQueue(),
            state=InMemoryAgentState(),
            engine=FakeReasoningEngine(),
            broker=FakeCapabilityBroker(),
            consent_context=ConsentContext(tenant_id=_TENANT, operator_id=None),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
        )

        pool.request_shutdown()
        await asyncio.wait_for(pool.run_forever(size=5), timeout=1.0)

        assert pool.configured_size() == 5, (
            f"Expected configured_size()=5, got {pool.configured_size()}"
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _wait_until(predicate, *, poll_interval: float = 0.005) -> None:
    """Poll predicate until it returns True, yielding to the event loop each tick.

    Used instead of asyncio.sleep(fixed) to avoid timing-dependent test failures.
    The caller wraps this in asyncio.wait_for() to enforce a hard deadline.
    """
    while not predicate():
        await asyncio.sleep(poll_interval)
