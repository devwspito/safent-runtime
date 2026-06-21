"""Unit tests — BrowserAdmissionGuard (Phase 2a RAM-safety layer).

Covers:
- capacity_from_fake_reader: capacity computed correctly from fake MemoryReader.
- backpressure_ordering: acquire blocks at capacity, unblocks on release; order
  matches FIFO semaphore semantics.
- dynamic_guard_parks: dynamic guard parks while mem < budget, proceeds on recovery.
- mem_unreadable_static_only: unreadable memory → static-semaphore-only, no crash.
- permit_leak_regression: failure between acquire and simulated spawn → capacity
  fully restored (no orphan permits).
- shutdown_aborts_waiting_acquire: BrowserAdmissionDenied raised if shutdown
  signaled while waiting.
- release_idempotent: releasing an unknown context_id is a safe no-op.
- capacity_clamp: capacity never exceeds hard_cap; always >= 1.
"""

from __future__ import annotations

import asyncio
from typing import Iterator
from unittest.mock import patch

import pytest

from hermes.execution.application.browser_admission_guard import (
    BrowserAdmissionDenied,
    BrowserAdmissionGuard,
    _compute_capacity,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake MemoryReader helpers
# ---------------------------------------------------------------------------


class _FixedMemReader:
    """Returns a fixed MB value (or None) for each call."""

    def __init__(self, mb_sequence: list[int | None]) -> None:
        self._seq = iter(mb_sequence)
        self._last: int | None = None

    def mem_available_mb(self) -> int | None:
        try:
            self._last = next(self._seq)
        except StopIteration:
            pass  # repeat last value when sequence exhausted
        return self._last


class _ConstantMemReader:
    """Returns the same MB value on every call."""

    def __init__(self, mb: int | None) -> None:
        self._mb = mb

    def mem_available_mb(self) -> int | None:
        return self._mb


# ---------------------------------------------------------------------------
# _compute_capacity (pure function, no I/O)
# ---------------------------------------------------------------------------


class TestComputeCapacity:
    def test_explicit_override_wins(self) -> None:
        cap = _compute_capacity(
            mem_available_mb=8000,
            session_mb=400,
            hard_cap=64,
            explicit_override=3,
            fallback=4,
        )
        assert cap == 3

    def test_derived_from_mem(self) -> None:
        # 4000 MB / 400 MB = 10 sessions
        cap = _compute_capacity(
            mem_available_mb=4000,
            session_mb=400,
            hard_cap=64,
            explicit_override=None,
            fallback=4,
        )
        assert cap == 10

    def test_floors_partial_session(self) -> None:
        # 999 MB / 400 MB = 2.49 → floor → 2
        cap = _compute_capacity(
            mem_available_mb=999,
            session_mb=400,
            hard_cap=64,
            explicit_override=None,
            fallback=4,
        )
        assert cap == 2

    def test_fallback_when_mem_unreadable(self) -> None:
        cap = _compute_capacity(
            mem_available_mb=None,
            session_mb=400,
            hard_cap=64,
            explicit_override=None,
            fallback=4,
        )
        assert cap == 4

    def test_clamped_to_hard_cap(self) -> None:
        cap = _compute_capacity(
            mem_available_mb=100_000,
            session_mb=400,
            hard_cap=64,
            explicit_override=None,
            fallback=4,
        )
        assert cap == 64

    def test_minimum_is_one(self) -> None:
        cap = _compute_capacity(
            mem_available_mb=10,
            session_mb=400,
            hard_cap=64,
            explicit_override=None,
            fallback=4,
        )
        assert cap == 1

    def test_explicit_override_clamped_to_hard_cap(self) -> None:
        cap = _compute_capacity(
            mem_available_mb=None,
            session_mb=400,
            hard_cap=8,
            explicit_override=100,
            fallback=4,
        )
        assert cap == 8


# ---------------------------------------------------------------------------
# BrowserAdmissionGuard — basic construction
# ---------------------------------------------------------------------------


class TestCapacityFromFakeReader:
    def test_capacity_matches_reader(self) -> None:
        # 2000 MB / 400 MB = 5 sessions
        reader = _ConstantMemReader(mb=2000)
        with patch.dict("os.environ", {
            "HERMES_BROWSER_SESSION_MB": "400",
            "HERMES_BROWSER_HARD_CAP": "64",
        }, clear=False):
            guard = BrowserAdmissionGuard(memory_reader=reader)
        assert guard.capacity() == 5

    def test_capacity_fallback_when_unreadable(self) -> None:
        reader = _ConstantMemReader(mb=None)
        with patch.dict("os.environ", {
            "HERMES_BROWSER_SESSION_MB": "400",
            "HERMES_BROWSER_HARD_CAP": "64",
        }, clear=False):
            guard = BrowserAdmissionGuard(memory_reader=reader)
        # fallback = 4
        assert guard.capacity() == 4

    def test_active_sessions_starts_at_zero(self) -> None:
        reader = _ConstantMemReader(mb=4000)
        guard = BrowserAdmissionGuard(memory_reader=reader)
        assert guard.active_sessions() == 0


# ---------------------------------------------------------------------------
# BrowserAdmissionGuard — acquire / release (semaphore backpressure)
# ---------------------------------------------------------------------------


class TestBackpressureOrdering:
    @pytest.mark.asyncio
    async def test_acquire_increments_active(self) -> None:
        reader = _ConstantMemReader(mb=4000)
        with patch.dict("os.environ", {
            "HERMES_BROWSER_SESSION_MB": "400",
            "HERMES_BROWSER_HARD_CAP": "64",
        }, clear=False):
            guard = BrowserAdmissionGuard(memory_reader=reader)

        await guard.acquire("ctx-1")
        assert guard.active_sessions() == 1

        await guard.acquire("ctx-2")
        assert guard.active_sessions() == 2

        guard.release("ctx-1")
        assert guard.active_sessions() == 1

        guard.release("ctx-2")
        assert guard.active_sessions() == 0

    @pytest.mark.asyncio
    async def test_blocks_at_capacity_unblocks_on_release(self) -> None:
        """At capacity=1, the second acquire waits until release."""
        reader = _ConstantMemReader(mb=400)  # exactly 1 session (400/400=1)
        with patch.dict("os.environ", {
            "HERMES_BROWSER_SESSION_MB": "400",
            "HERMES_BROWSER_HARD_CAP": "64",
            "HERMES_BROWSER_HEADROOM_MB": "0",  # disable dynamic guard for this test
        }, clear=False):
            guard = BrowserAdmissionGuard(
                memory_reader=reader, recheck_interval_s=0.01
            )

        assert guard.capacity() == 1

        acquired_order: list[str] = []

        async def task1() -> None:
            await guard.acquire("ctx-1")
            acquired_order.append("ctx-1")

        async def task2() -> None:
            # Wait a moment so task1 acquires first
            await asyncio.sleep(0.02)
            await guard.acquire("ctx-2")
            acquired_order.append("ctx-2")

        t1 = asyncio.create_task(task1())
        t2 = asyncio.create_task(task2())

        # Let both tasks start; ctx-2 should be waiting
        await asyncio.sleep(0.05)
        assert guard.active_sessions() == 1
        assert "ctx-1" in acquired_order
        assert "ctx-2" not in acquired_order

        # Release ctx-1 → ctx-2 should proceed
        guard.release("ctx-1")
        await asyncio.gather(t1, t2)

        assert guard.active_sessions() == 1
        assert acquired_order == ["ctx-1", "ctx-2"]

        guard.release("ctx-2")
        assert guard.active_sessions() == 0


# ---------------------------------------------------------------------------
# BrowserAdmissionGuard — dynamic memory guard
# ---------------------------------------------------------------------------


class TestDynamicGuardParksOnPressure:
    @pytest.mark.asyncio
    async def test_parks_while_mem_below_budget_then_proceeds(self) -> None:
        """Dynamic guard parks when mem < session_mb + headroom_mb, resumes on recovery."""
        # Sequence: first two reads are low (parking), third is high (proceed)
        reader = _FixedMemReader([100, 100, 5000])

        with patch.dict("os.environ", {
            "HERMES_BROWSER_SESSION_MB": "400",
            "HERMES_BROWSER_HARD_CAP": "64",
            "HERMES_BROWSER_HEADROOM_MB": "512",  # budget = 912 MB
        }, clear=False):
            guard = BrowserAdmissionGuard(
                memory_reader=reader,
                recheck_interval_s=0.01,
            )

        # Should complete once reader returns 5000 MB (> 912 MB budget)
        await asyncio.wait_for(guard.acquire("ctx-test"), timeout=1.0)
        assert guard.active_sessions() == 1
        guard.release("ctx-test")

    @pytest.mark.asyncio
    async def test_no_park_when_mem_above_budget(self) -> None:
        """No parking when initial mem is above budget."""
        reader = _ConstantMemReader(mb=5000)
        with patch.dict("os.environ", {
            "HERMES_BROWSER_SESSION_MB": "400",
            "HERMES_BROWSER_HARD_CAP": "64",
            "HERMES_BROWSER_HEADROOM_MB": "512",
        }, clear=False):
            guard = BrowserAdmissionGuard(memory_reader=reader)

        await asyncio.wait_for(guard.acquire("ctx-fast"), timeout=0.5)
        assert guard.active_sessions() == 1
        guard.release("ctx-fast")


# ---------------------------------------------------------------------------
# BrowserAdmissionGuard — unreadable memory (static-semaphore-only)
# ---------------------------------------------------------------------------


class TestMemUnreadableStaticOnly:
    @pytest.mark.asyncio
    async def test_no_crash_when_mem_unreadable(self) -> None:
        """If MemAvailable is always None, static semaphore applies — no crash."""
        reader = _ConstantMemReader(mb=None)
        with patch.dict("os.environ", {
            "HERMES_BROWSER_SESSION_MB": "400",
            "HERMES_BROWSER_HARD_CAP": "64",
        }, clear=False):
            guard = BrowserAdmissionGuard(memory_reader=reader)

        # Should acquire and release without raising
        await asyncio.wait_for(guard.acquire("ctx-noreader"), timeout=0.5)
        assert guard.active_sessions() == 1
        guard.release("ctx-noreader")
        assert guard.active_sessions() == 0

    def test_capacity_is_fallback_when_unreadable(self) -> None:
        reader = _ConstantMemReader(mb=None)
        guard = BrowserAdmissionGuard(memory_reader=reader)
        assert guard.capacity() == 4  # fallback constant


# ---------------------------------------------------------------------------
# BrowserAdmissionGuard — permit leak regression
# ---------------------------------------------------------------------------


class TestPermitLeakRegression:
    @pytest.mark.asyncio
    async def test_failure_after_acquire_restores_capacity(self) -> None:
        """Simulates: acquire succeeds, then _start_os_resource raises.
        The caller must release the permit on the exception path.
        After release, capacity is fully restored.
        """
        reader = _ConstantMemReader(mb=4000)
        with patch.dict("os.environ", {
            "HERMES_BROWSER_SESSION_MB": "400",
            "HERMES_BROWSER_HARD_CAP": "64",
            "HERMES_BROWSER_HEADROOM_MB": "0",
        }, clear=False):
            guard = BrowserAdmissionGuard(memory_reader=reader)

        initial_capacity = guard.capacity()

        context_id = "ctx-failure"
        acquired = False
        try:
            await guard.acquire(context_id)
            acquired = True
            assert guard.active_sessions() == 1
            # Simulate OS spawn failure
            raise RuntimeError("simulated _start_os_resource failure")
        except RuntimeError:
            if acquired:
                guard.release(context_id)

        # Capacity fully restored — no orphan permit
        assert guard.active_sessions() == 0

        # Can acquire again at full capacity
        tasks = [guard.acquire(f"ctx-new-{i}") for i in range(min(initial_capacity, 3))]
        await asyncio.gather(*tasks)
        assert guard.active_sessions() == min(initial_capacity, 3)
        for i in range(min(initial_capacity, 3)):
            guard.release(f"ctx-new-{i}")
        assert guard.active_sessions() == 0


# ---------------------------------------------------------------------------
# BrowserAdmissionGuard — shutdown abort
# ---------------------------------------------------------------------------


class TestShutdownAbort:
    @pytest.mark.asyncio
    async def test_shutdown_aborts_waiting_acquire(self) -> None:
        """BrowserAdmissionDenied raised when shutdown signaled during wait."""
        reader = _ConstantMemReader(mb=400)
        with patch.dict("os.environ", {
            "HERMES_BROWSER_SESSION_MB": "400",
            "HERMES_BROWSER_HARD_CAP": "64",
            "HERMES_BROWSER_HEADROOM_MB": "0",
        }, clear=False):
            guard = BrowserAdmissionGuard(
                memory_reader=reader, recheck_interval_s=0.01
            )

        assert guard.capacity() == 1

        # Fill the semaphore
        await guard.acquire("ctx-blocker")

        async def blocked_acquire() -> None:
            await guard.acquire("ctx-waiting")

        task = asyncio.create_task(blocked_acquire())
        await asyncio.sleep(0.02)  # let it start waiting

        guard.signal_shutdown()

        with pytest.raises(BrowserAdmissionDenied):
            await asyncio.wait_for(task, timeout=1.0)

        # Capacity restored after aborted acquire
        assert guard.active_sessions() == 1  # only the blocker remains
        guard.release("ctx-blocker")
        assert guard.active_sessions() == 0


# ---------------------------------------------------------------------------
# BrowserAdmissionGuard — idempotent release
# ---------------------------------------------------------------------------


class TestReleaseIdempotent:
    def test_releasing_unknown_context_id_is_noop(self) -> None:
        reader = _ConstantMemReader(mb=4000)
        guard = BrowserAdmissionGuard(memory_reader=reader)
        # Should not raise
        guard.release("ctx-never-acquired")

    @pytest.mark.asyncio
    async def test_double_release_is_safe(self) -> None:
        """Releasing the same context_id twice does not corrupt the semaphore."""
        reader = _ConstantMemReader(mb=4000)
        with patch.dict("os.environ", {
            "HERMES_BROWSER_SESSION_MB": "400",
            "HERMES_BROWSER_HEADROOM_MB": "0",
        }, clear=False):
            guard = BrowserAdmissionGuard(memory_reader=reader)

        await guard.acquire("ctx-double")
        guard.release("ctx-double")  # first release
        guard.release("ctx-double")  # idempotent second release

        assert guard.active_sessions() == 0
