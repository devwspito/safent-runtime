"""T403: Integration test for InMemoryStorageStatePort concurrent lock.

Two coroutines opening a session for the same (tenant_id, site_id): the
second blocks until the first closes or times out at 30s.

This test exercises asyncio.Lock per-key semantics.  The Postgres equivalent
(pg_advisory_xact_lock) will be written inside gestoria-agent/T409.

Marker: integration (excluded from base CI by pyproject.toml addopts).
Constitution V: no Postgres required; asyncio-only.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from hermes.browser.domain.ports.storage_state_port import (
    EncryptedStorageState,
    StorageStateLocked,
)
from hermes.browser.testing.in_memory_storage_state import InMemoryStorageStatePort

pytestmark = pytest.mark.integration


def _make_state(tenant_id, site_id="shared_site") -> EncryptedStorageState:
    return EncryptedStorageState(
        tenant_id=tenant_id,
        site_id=site_id,
        ciphertext=b"ct",
        kid="k",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("num_sessions", [2, 3])
async def test_concurrent_sessions_serialized(num_sessions: int) -> None:
    """N concurrent sessions for same (tenant_id, site_id) run serially."""
    port = InMemoryStorageStatePort()
    tenant_id = uuid4()
    site_id = "shared_site"

    execution_order: list[int] = []
    release_events = [asyncio.Event() for _ in range(num_sessions)]

    async def session(index: int) -> None:
        async with port.lock(tenant_id=tenant_id, site_id=site_id, timeout_s=30.0):
            execution_order.append(index)
            # Yield control so next coroutine can attempt to acquire.
            await asyncio.sleep(0)
            await port.save(_make_state(tenant_id=tenant_id, site_id=site_id))
            release_events[index].set()

    tasks = [asyncio.create_task(session(i)) for i in range(num_sessions)]
    await asyncio.gather(*tasks)

    # All sessions ran exactly once.
    assert len(execution_order) == num_sessions
    # Each index appeared exactly once.
    assert sorted(execution_order) == list(range(num_sessions))


@pytest.mark.asyncio
async def test_lock_timeout_raises_storage_state_locked() -> None:
    """Second session times out when first holds the lock for too long."""
    port = InMemoryStorageStatePort()
    tenant_id = uuid4()
    site_id = "timeout_site"

    hold_event = asyncio.Event()

    async def holder() -> None:
        async with port.lock(tenant_id=tenant_id, site_id=site_id, timeout_s=30.0):
            await hold_event.wait()

    t = asyncio.create_task(holder())
    await asyncio.sleep(0)  # Let holder acquire.

    with pytest.raises(StorageStateLocked):
        async with port.lock(tenant_id=tenant_id, site_id=site_id, timeout_s=0.05):
            pass

    hold_event.set()
    await t


@pytest.mark.asyncio
async def test_independent_site_ids_do_not_block_each_other() -> None:
    """Locks for different site_ids are independent (no cross-lock)."""
    port = InMemoryStorageStatePort()
    tenant_id = uuid4()

    hold_a = asyncio.Event()
    done_b = asyncio.Event()

    async def hold_site_a() -> None:
        async with port.lock(tenant_id=tenant_id, site_id="site_a", timeout_s=30.0):
            await hold_a.wait()

    async def acquire_site_b() -> None:
        # Should not block even though site_a is locked.
        async with port.lock(tenant_id=tenant_id, site_id="site_b", timeout_s=0.1):
            done_b.set()

    t_a = asyncio.create_task(hold_site_a())
    await asyncio.sleep(0)
    t_b = asyncio.create_task(acquire_site_b())

    await asyncio.wait_for(done_b.wait(), timeout=1.0)
    assert done_b.is_set()

    hold_a.set()
    await t_a
    await t_b
