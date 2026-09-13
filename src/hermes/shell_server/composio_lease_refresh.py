"""Shell lifecycle for the daemon's fixed-purpose local Ads lease publisher."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress


class ComposioLeaseRefresh:
    def __init__(self, dbus_proxy, *, interval: float = 20) -> None:
        self._proxy = dbus_proxy
        self._interval = interval
        self._next_attempt = 0.0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def ensure(self, *, force: bool = False) -> None:
        async with self._lock:
            if not force and time.monotonic() < self._next_attempt:
                return
            self._next_attempt = time.monotonic() + self._interval
            try:
                async with asyncio.timeout(30):
                    await self._proxy.call_dict("publish_companion_composio_lease")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - no secret-bearing upstream text or boot failure
                return

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await self.ensure()
            await asyncio.sleep(self._interval)
