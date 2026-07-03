"""Regression tests for teach_vnc._Recorder resilience.

Bug: POST /api/v1/teach/{sid}/save returned 409 "no steps were captured" EVERY
time, even though the owner demonstrably drove the noVNC browser. Root cause
(reproduced E2E on a live container): the shared jailed Chromium crashes and
JailedBrowserManager.ensure_running() transparently RESPAWNS a brand new
process on the same CDP URL. _Recorder held a single Playwright Browser handle
from its initial connect_over_cdp() and never noticed the swap, so every step
demonstrated on the NEW process landed on zero listeners -> capture_step was
never called -> compile_and_persist saw 0 steps -> 409.

Fix: a periodic rescan tick that (a) re-wires any context/page not yet wired
(covers a new tab/context within the SAME connection) and (b) reconnects via
connect_over_cdp when the current connection has dropped (covers the
crash-respawn case proven above).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hermes.shell_server.cowork.teach_vnc import _Recorder


class _FakeCdpSession:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self._handlers: dict[str, object] = {}

    async def send(self, method: str, params: dict | None = None) -> None:
        self.sent.append((method, params or {}))

    def on(self, event: str, handler) -> None:
        self._handlers[event] = handler


class _FakePage:
    def __init__(self, ctx: "_FakeContext") -> None:
        self.context = ctx
        self.evaluated: list[str] = []
        self._handlers: dict[str, object] = {}

    def on(self, event: str, handler) -> None:
        self._handlers[event] = handler

    async def evaluate(self, script: str) -> None:
        self.evaluated.append(script)


class _FakeContext:
    def __init__(self, pages: "list[_FakePage] | None" = None) -> None:
        self.pages: list[_FakePage] = pages or []
        self._page_handlers: list[object] = []

    def on(self, event: str, handler) -> None:
        if event == "page":
            self._page_handlers.append(handler)

    async def new_cdp_session(self, page: _FakePage) -> _FakeCdpSession:
        return _FakeCdpSession()


class _FakeBrowser:
    def __init__(self, contexts: "list[_FakeContext]", connected: bool = True) -> None:
        self.contexts = contexts
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def capture_step(self, *, session_id, surface_kind, action_payload) -> None:
        self.calls.append(
            {"session_id": session_id, "surface_kind": surface_kind, "payload": action_payload}
        )


def _make_recorder(orch: _FakeOrchestrator) -> _Recorder:
    rec = _Recorder(cdp_url="http://fake/cdp", orchestrator=orch, session_id="sid-1")
    return rec


@pytest.mark.asyncio
async def test_rescan_tick_wires_a_new_page_in_an_already_wired_context() -> None:
    """A page opened AFTER connect (e.g. a new tab) in the SAME browser
    connection is picked up by the next rescan tick, not just at start()."""
    orch = _FakeOrchestrator()
    rec = _make_recorder(orch)

    ctx = _FakeContext(pages=[])
    browser = _FakeBrowser(contexts=[ctx], connected=True)
    rec._browser = browser

    await rec._wire_all_contexts()
    assert rec._wired == set()

    # A new page appears in the same, already-wired context.
    new_page = _FakePage(ctx)
    ctx.pages.append(new_page)

    await rec._rescan_tick()

    assert id(new_page) in rec._wired
    assert new_page.evaluated  # OBSERVER_JS was injected


@pytest.mark.asyncio
async def test_rescan_tick_reconnects_after_shared_browser_crash_respawn() -> None:
    """Regression: once the current CDP connection reports disconnected (the
    shared jailed browser crashed and JailedBrowserManager respawned a NEW
    process), the next tick must reconnect and wire the NEW browser's pages —
    not silently keep recording nothing, which produced the 409 bug."""
    orch = _FakeOrchestrator()
    rec = _make_recorder(orch)

    old_ctx = _FakeContext(pages=[_FakePage(None)])
    old_browser = _FakeBrowser(contexts=[old_ctx], connected=True)
    rec._browser = old_browser
    old_ctx.pages[0].context = old_ctx
    await rec._wire_all_contexts()
    assert len(rec._wired) == 1

    # Simulate the crash: the OLD connection is now dead.
    old_browser._connected = False

    # A brand-new Chromium process (post-respawn) with its own fresh page —
    # this is what the operator's demonstration actually lands on.
    new_ctx = _FakeContext()
    new_page = _FakePage(new_ctx)
    new_ctx.pages.append(new_page)
    new_browser = _FakeBrowser(contexts=[new_ctx], connected=True)

    rec._connect = AsyncMock(return_value=new_browser)

    await rec._rescan_tick()

    rec._connect.assert_awaited_once()
    assert rec._browser is new_browser
    assert id(new_page) in rec._wired

    # The demonstrated step on the NEW page must now reach the orchestrator —
    # before the fix this path was never wired and capture_step was never
    # called, producing 0 steps -> compile_and_persist False -> /save 409.
    rec._capture({"type": "navigate", "url": "https://example.com"})
    assert orch.calls
    assert orch.calls[0]["payload"] == {"kind": "navigate", "url": "https://example.com"}


@pytest.mark.asyncio
async def test_rescan_tick_is_fail_soft_on_scan_error() -> None:
    """A scan/reconnect error must never propagate — the loop keeps running so
    a transient blip doesn't permanently kill the recording."""
    orch = _FakeOrchestrator()
    rec = _make_recorder(orch)

    broken_browser = _FakeBrowser(contexts=[], connected=False)
    rec._browser = broken_browser
    rec._connect = AsyncMock(side_effect=RuntimeError("CDP unreachable"))

    await rec._rescan_tick()  # must not raise

    rec._connect.assert_awaited_once()
