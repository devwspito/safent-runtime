from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes.shell_server.composio_lease_refresh import ComposioLeaseRefresh

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_background_initial_refresh_and_stop_are_idempotent():
    called = asyncio.Event()
    cancelled = asyncio.Event()

    async def publish(_):
        called.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    refresh = ComposioLeaseRefresh(SimpleNamespace(call_dict=publish))
    refresh.start()
    task = refresh._task
    refresh.start()
    assert refresh._task is task
    await asyncio.wait_for(called.wait(), 1)
    await refresh.stop()
    assert cancelled.is_set()
    assert refresh._task is None
    await refresh.stop()


@pytest.mark.asyncio
async def test_ensure_serializes_rate_limits_and_force_applies_configuration():
    proxy = SimpleNamespace(call_dict=AsyncMock(return_value={"accepted": True}))
    refresh = ComposioLeaseRefresh(proxy)
    await asyncio.gather(*(refresh.ensure() for _ in range(10)))
    assert proxy.call_dict.await_count == 1
    await refresh.ensure(force=True)
    assert proxy.call_dict.await_count == 2
    assert proxy.call_dict.await_args.args == ("publish_companion_composio_lease",)


@pytest.mark.asyncio
async def test_daemon_absence_is_fail_soft():
    proxy = SimpleNamespace(
        call_dict=AsyncMock(side_effect=RuntimeError("fixture-sensitive-error"))
    )
    refresh = ComposioLeaseRefresh(proxy)
    assert await refresh.ensure() is None
    assert await refresh.ensure() is None
    assert proxy.call_dict.await_count == 1
