"""P3 — Daemon liveness wiring tests (US2, SC-3, feature 008).

Tests-first: these tests FAIL before ModelHealthMonitor is wired in __main__.

Validates:
- ModelHealthMonitor emits AgentLivenessChanged(has_model=False) when endpoint
  is unreachable — via a fake D-Bus emitter (no real bus required).
- ModelHealthMonitor emits has_model=True when endpoint is healthy.
- The monitor's on_state_change correctly drives emit_liveness_changed with
  alive=True (agent process is alive) + has_model reflecting endpoint state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeHttpClient:
    """Fake HTTP client — controls what ModelHealthMonitor._tick() sees."""

    def __init__(self) -> None:
        self._responses: list[bool | Exception] = []
        self.call_count: int = 0

    def queue_ok(self) -> None:
        self._responses.append(True)

    def queue_error(self, exc: Exception | None = None) -> None:
        self._responses.append(exc or ConnectionRefusedError("llm gone"))

    def queue_many_errors(self, n: int) -> None:
        for _ in range(n):
            self.queue_error()

    async def is_healthy(self) -> bool:
        self.call_count += 1
        if not self._responses:
            return True
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return bool(resp)


@dataclass
class LivenessEvent:
    alive: bool
    has_model: bool


class FakeDbusAdapter:
    """Records every call to emit_liveness_changed (no real D-Bus)."""

    def __init__(self) -> None:
        self.events: list[LivenessEvent] = []

    def emit_liveness_changed(self, *, alive: bool, has_model: bool) -> None:
        self.events.append(LivenessEvent(alive=alive, has_model=has_model))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fast_config(**overrides: Any):
    from hermes.runtime.model_health_monitor import ModelMonitorConfig

    base = ModelMonitorConfig(
        poll_interval_s=0.01,
        backoff_initial_s=0.01,
        backoff_cap_s=0.08,
        backoff_multiplier=2.0,
        grace_period_s=0.02,
        max_retries_before_offline=2,
    )
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


def _build_monitor_with_adapter(
    http: FakeHttpClient,
    adapter: FakeDbusAdapter,
    config=None,
):
    """Wire ModelHealthMonitor so its on_state_change calls adapter.emit_liveness_changed."""
    from hermes.runtime.model_health_monitor import ModelHealthMonitor
    from hermes.shell.domain.shell_session import RuntimeLinkState

    def _on_state_change(state) -> None:
        has_model = state == RuntimeLinkState.CONNECTED
        adapter.emit_liveness_changed(alive=True, has_model=has_model)

    return ModelHealthMonitor(
        http_client=http,
        config=config or _fast_config(),
        on_state_change=_on_state_change,
    )


async def _run_briefly(monitor, *, seconds: float) -> None:
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_liveness_has_model_true_when_endpoint_healthy() -> None:
    """AgentLivenessChanged(has_model=True) emitted when /v1/models responds."""
    http = FakeHttpClient()
    adapter = FakeDbusAdapter()
    monitor = _build_monitor_with_adapter(http, adapter)

    http.queue_ok()
    await _run_briefly(monitor, seconds=0.05)

    has_model_true_events = [e for e in adapter.events if e.has_model]
    assert len(has_model_true_events) >= 1
    assert all(e.alive for e in has_model_true_events)


async def test_liveness_has_model_false_when_endpoint_down() -> None:
    """AgentLivenessChanged(has_model=False) emitted when LLM endpoint is unreachable."""
    http = FakeHttpClient()
    adapter = FakeDbusAdapter()
    cfg = _fast_config(max_retries_before_offline=2, grace_period_s=0.01)
    monitor = _build_monitor_with_adapter(http, adapter, config=cfg)

    http.queue_ok()           # first tick -> CONNECTED
    http.queue_many_errors(8) # subsequent ticks -> OFFLINE

    await _run_briefly(monitor, seconds=0.4)

    has_model_false_events = [e for e in adapter.events if not e.has_model]
    assert len(has_model_false_events) >= 1, (
        "Expected at least one AgentLivenessChanged(has_model=False) when "
        f"endpoint is down. Got events: {adapter.events}"
    )


async def test_liveness_transitions_false_then_true_on_recovery() -> None:
    """AgentLivenessChanged reflects full lifecycle: healthy -> down -> healthy."""
    http = FakeHttpClient()
    adapter = FakeDbusAdapter()
    cfg = _fast_config(max_retries_before_offline=2, grace_period_s=0.01)
    monitor = _build_monitor_with_adapter(http, adapter, config=cfg)

    http.queue_ok()
    http.queue_many_errors(6)
    http.queue_ok()
    http.queue_ok()
    http.queue_ok()

    await _run_briefly(monitor, seconds=0.8)

    # Must see has_model=True, then has_model=False, then has_model=True again
    has_model_values = [e.has_model for e in adapter.events]
    assert True in has_model_values, "Expected at least one has_model=True event"
    assert False in has_model_values, "Expected at least one has_model=False event"

    # The last meaningful transition must be to has_model=True (recovered)
    last_true = max(i for i, v in enumerate(has_model_values) if v)
    last_false = max(i for i, v in enumerate(has_model_values) if not v)
    assert last_true > last_false, (
        "Expected has_model=True after the last has_model=False (recovery)"
    )


async def test_alive_always_true_regardless_of_model_health() -> None:
    """alive=True in all events — the daemon process itself is running."""
    http = FakeHttpClient()
    adapter = FakeDbusAdapter()
    cfg = _fast_config(max_retries_before_offline=2, grace_period_s=0.01)
    monitor = _build_monitor_with_adapter(http, adapter, config=cfg)

    http.queue_ok()
    http.queue_many_errors(8)

    await _run_briefly(monitor, seconds=0.4)

    assert adapter.events, "Expected at least one liveness event"
    assert all(e.alive for e in adapter.events), (
        "alive must always be True from ModelHealthMonitor — daemon is alive"
    )
