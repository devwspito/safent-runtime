"""ComposioToolsRegistry: TTL cache + dynamic reload unit tests.

Cases:
  (a) Returns composio tools from a fresh (cold) cache.
  (b) Cache hit within TTL — no re-fetch.
  (c) Stale cache → re-fetch picks up newly connected app.
  (d) Fail-soft: refresh error keeps last-good cache; never raises.
  (d-bis) Fail-soft: timeout on Composio fetch serves last-good cache.
  (e) Credential appearing later (None → present) starts returning Composio tools.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from hermes.domain.tool_spec import ToolRisk, ToolSpec
from hermes.runtime.composio_config_source import ComposioCredential
from hermes.runtime.composio_tools_registry import (
    ComposioToolsRegistry,
    _REFRESH_TIMEOUT_S,
)

pytestmark = pytest.mark.unit

_DB = Path("/tmp/test-composio-registry.db")
_CRED = ComposioCredential(api_key="csk-test", entity_id="ent-test")


def _make_spec(name: str) -> ToolSpec:
    async def _handler(_: dict[str, Any]) -> dict[str, Any]:
        return {}

    return ToolSpec(
        name=name,
        description=f"Tool {name}",
        parameters_schema={"type": "object", "properties": {}},
        risk=ToolRisk.READ_ONLY,
        entity_type="composio",
        handler=_handler,
        tags=("composio",),
    )


_GMAIL_SPEC = _make_spec("gmail_get_email")
_SLACK_SPEC = _make_spec("slack_fetch_messages")


def _registry(
    *,
    ttl_s: float = 60.0,
    cred: ComposioCredential | None = _CRED,
    specs: tuple[ToolSpec, ...] = (_GMAIL_SPEC,),
) -> ComposioToolsRegistry:
    """Build a registry with simple in-memory stubs."""

    async def _build(c: ComposioCredential) -> tuple[ToolSpec, ...]:
        return specs

    return ComposioToolsRegistry(
        db_path=_DB,
        ttl_s=ttl_s,
        credential_loader=lambda _db: cred,
        tools_builder=_build,
    )


# -----------------------------------------------------------------------
# (a) Returns composio tools on a fresh (cold) cache
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_composio_tools_on_first_call() -> None:
    registry = _registry()
    tools = await registry.get_composio_tools()
    assert _GMAIL_SPEC in tools


# -----------------------------------------------------------------------
# (b) Cache hit within TTL — no re-fetch
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_refetch_within_ttl() -> None:
    call_count = {"n": 0}

    async def _counting_builder(cred: ComposioCredential) -> tuple[ToolSpec, ...]:
        call_count["n"] += 1
        return (_GMAIL_SPEC,)

    registry = ComposioToolsRegistry(
        db_path=_DB,
        ttl_s=60.0,
        credential_loader=lambda _: _CRED,
        tools_builder=_counting_builder,
    )

    await registry.get_composio_tools()
    await registry.get_composio_tools()
    await registry.get_composio_tools()

    assert call_count["n"] == 1, "should fetch once and cache for subsequent calls"


# -----------------------------------------------------------------------
# (c) Stale cache → re-fetch picks up newly connected app
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refetch_after_ttl_picks_up_new_app() -> None:
    call_count = {"n": 0}

    async def _evolving_builder(cred: ComposioCredential) -> tuple[ToolSpec, ...]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return (_GMAIL_SPEC,)
        # Second fetch: user connected Slack
        return (_GMAIL_SPEC, _SLACK_SPEC)

    registry = ComposioToolsRegistry(
        db_path=_DB,
        ttl_s=0.05,  # 50 ms TTL
        credential_loader=lambda _: _CRED,
        tools_builder=_evolving_builder,
    )

    first = await registry.get_composio_tools()
    assert _GMAIL_SPEC in first
    assert _SLACK_SPEC not in first

    await asyncio.sleep(0.06)

    second = await registry.get_composio_tools()

    assert _SLACK_SPEC in second, "newly connected Slack app must appear after TTL"
    assert call_count["n"] == 2


# -----------------------------------------------------------------------
# (d) Fail-soft: refresh error keeps last-good cache; never raises
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_error_serves_last_good_cache() -> None:
    call_count = {"n": 0}

    async def _flaky_builder(cred: ComposioCredential) -> tuple[ToolSpec, ...]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return (_GMAIL_SPEC,)
        raise RuntimeError("Composio API unreachable")

    registry = ComposioToolsRegistry(
        db_path=_DB,
        ttl_s=0.05,
        credential_loader=lambda _: _CRED,
        tools_builder=_flaky_builder,
    )

    first = await registry.get_composio_tools()
    assert _GMAIL_SPEC in first

    await asyncio.sleep(0.06)

    # Must not raise; must serve last-good
    second = await registry.get_composio_tools()

    assert _GMAIL_SPEC in second, "last-good cache must survive a refresh failure"
    assert call_count["n"] == 2


# -----------------------------------------------------------------------
# (d-bis) Fail-soft: timeout on Composio fetch serves last-good cache
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_timeout_serves_last_good_cache() -> None:
    call_count = {"n": 0}

    async def _slow_builder(cred: ComposioCredential) -> tuple[ToolSpec, ...]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return (_GMAIL_SPEC,)
        await asyncio.sleep(10)  # will be cancelled by the bounded timeout
        return ()

    registry = ComposioToolsRegistry(
        db_path=_DB,
        ttl_s=0.05,
        credential_loader=lambda _: _CRED,
        tools_builder=_slow_builder,
    )
    # Override the module-level timeout so the test doesn't actually wait 10 s.
    # We patch the constant in the registry's _refresh via monkeypatching the
    # module attribute used in the wait_for call.
    import hermes.runtime.composio_tools_registry as _reg_mod

    original_timeout = _reg_mod._REFRESH_TIMEOUT_S
    _reg_mod._REFRESH_TIMEOUT_S = 0.02
    try:
        first = await registry.get_composio_tools()
        await asyncio.sleep(0.06)
        second = await registry.get_composio_tools()
    finally:
        _reg_mod._REFRESH_TIMEOUT_S = original_timeout

    assert _GMAIL_SPEC in second, "timed-out refresh must fall back to last-good cache"


# -----------------------------------------------------------------------
# (e) Credential appearing later (None → present) starts returning tools
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_credential_appearing_later_yields_composio_tools() -> None:
    cred_holder: list[ComposioCredential | None] = [None]
    call_count = {"n": 0}

    async def _build(cred: ComposioCredential) -> tuple[ToolSpec, ...]:
        call_count["n"] += 1
        return (_GMAIL_SPEC,)

    registry = ComposioToolsRegistry(
        db_path=_DB,
        ttl_s=0.05,
        credential_loader=lambda _db: cred_holder[0],
        tools_builder=_build,
    )

    # First call: no credential → empty
    first = await registry.get_composio_tools()
    assert first == ()
    assert call_count["n"] == 0

    # Credential appears (user configures Composio API key during onboarding)
    cred_holder[0] = _CRED
    await asyncio.sleep(0.06)  # expire TTL

    second = await registry.get_composio_tools()

    assert _GMAIL_SPEC in second, "tools must appear once credential is configured"
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Background poller (_composio_poller in runtime.__main__) — realtime discovery
# ---------------------------------------------------------------------------


class _PollFakeRegistry:
    """Minimal registry stand-in: counts get_composio_tools() calls."""

    _ttl_s = 0.05

    def __init__(self, *, raise_on_call: bool = False) -> None:
        self.calls = 0
        self._raise = raise_on_call

    async def get_composio_tools(self) -> tuple:
        self.calls += 1
        if self._raise:
            raise RuntimeError("composio down")
        return ()


async def _run_poller_briefly(reg, *, seconds: float = 0.22) -> None:
    from hermes.runtime.__main__ import _composio_poller  # noqa: PLC0415

    task = asyncio.create_task(_composio_poller(reg))
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_poller_refreshes_registry_proactively() -> None:
    """The background poller calls get_composio_tools() repeatedly (proactive
    realtime discovery — no chat needed)."""
    reg = _PollFakeRegistry()
    await _run_poller_briefly(reg)
    assert reg.calls >= 2  # multiple ticks over ~0.22s at 0.05s interval


@pytest.mark.asyncio
async def test_poller_is_failsoft_and_keeps_polling() -> None:
    """A failing refresh never stops the poller (fail-soft) — it keeps ticking."""
    reg = _PollFakeRegistry(raise_on_call=True)
    await _run_poller_briefly(reg)
    assert reg.calls >= 2  # kept polling despite errors on every tick
