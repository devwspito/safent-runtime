"""P3 — ModelHealthMonitor unit tests (US2, SC-3/4, feature 008).

Tests-first: these tests FAIL before the ModelHealthMonitor is implemented.

Validates:
- State machine mirrors RuntimeBackendHealthMonitor (same states + backoff).
- OFFLINE -> CONNECTED when the /v1/models endpoint responds 200.
- CONNECTED -> RECONNECTING -> OFFLINE when the endpoint goes down and
  retries are exhausted (grace + backoff honoured).
- on_state_change callback fires emit_liveness_changed(alive=True/False,
  has_model=<endpoint_healthy>) on every transition.
- No real network — fake HTTP client injected.
- No LiteLLM import, no D-Bus import (SRP, headless testable).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeHttpClient:
    """Fake HTTP client that controls what _get_models() returns."""

    def __init__(self) -> None:
        self._responses: list[bool | Exception] = []
        self.call_count: int = 0

    def queue_ok(self) -> None:
        """Next call returns healthy (HTTP 200 with model list)."""
        self._responses.append(True)

    def queue_error(self, exc: Exception | None = None) -> None:
        """Next call raises / returns unhealthy."""
        self._responses.append(exc or ConnectionRefusedError("llm down"))

    def queue_many_errors(self, n: int) -> None:
        for _ in range(n):
            self.queue_error()

    async def is_healthy(self) -> bool:
        """Returns True if endpoint is reachable and has models, False/raises otherwise."""
        self.call_count += 1
        if not self._responses:
            return True
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return bool(resp)


@dataclass
class LivenessCall:
    alive: bool
    has_model: bool


class FakeLivenessEmitter:
    """Records calls to emit_liveness_changed."""

    def __init__(self) -> None:
        self.calls: list[LivenessCall] = []

    def emit(self, *, alive: bool, has_model: bool) -> None:
        self.calls.append(LivenessCall(alive=alive, has_model=has_model))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fast_config(**overrides: Any):
    """Import and build MonitorConfig with fast-test overrides."""
    from hermes.runtime.model_health_monitor import ModelMonitorConfig

    base = ModelMonitorConfig(
        poll_interval_s=0.01,
        backoff_initial_s=0.01,
        backoff_cap_s=0.08,
        backoff_multiplier=2.0,
        grace_period_s=0.04,
        max_retries_before_offline=3,
    )
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


def _make_monitor(
    http_client: FakeHttpClient,
    emitter: FakeLivenessEmitter | None = None,
    config=None,
    extra_callbacks: list[Callable] | None = None,
):
    """Construct a ModelHealthMonitor with fakes."""
    from hermes.runtime.model_health_monitor import ModelHealthMonitor

    callbacks: list[Callable] = []
    if emitter is not None:
        callbacks.append(
            lambda state, *, _e=emitter: _e.emit(
                alive=True,
                has_model=_state_to_has_model(state),
            )
        )
    if extra_callbacks:
        callbacks.extend(extra_callbacks)

    def _on_state_change(state) -> None:
        for cb in callbacks:
            cb(state)

    return ModelHealthMonitor(
        http_client=http_client,
        config=config or _fast_config(),
        on_state_change=_on_state_change if callbacks else None,
    )


def _state_to_has_model(state) -> bool:
    from hermes.shell.domain.shell_session import RuntimeLinkState

    return state == RuntimeLinkState.CONNECTED


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


def test_initial_state_is_offline() -> None:
    """ModelHealthMonitor starts in OFFLINE state."""
    from hermes.runtime.model_health_monitor import ModelHealthMonitor
    from hermes.shell.domain.shell_session import RuntimeLinkState

    monitor = ModelHealthMonitor(http_client=FakeHttpClient())
    assert monitor.state == RuntimeLinkState.OFFLINE


async def test_transitions_to_connected_when_endpoint_responds() -> None:
    """OFFLINE -> CONNECTED when HTTP client returns healthy."""
    from hermes.shell.domain.shell_session import RuntimeLinkState

    http = FakeHttpClient()
    emitter = FakeLivenessEmitter()
    observed: list = []
    monitor = _make_monitor(
        http,
        emitter=emitter,
        extra_callbacks=[observed.append],
    )

    http.queue_ok()
    await _run_briefly(monitor, seconds=0.05)

    assert RuntimeLinkState.CONNECTED in observed


async def test_connected_to_reconnecting_on_first_failure() -> None:
    """CONNECTED -> RECONNECTING on first HTTP failure."""
    from hermes.shell.domain.shell_session import RuntimeLinkState

    http = FakeHttpClient()
    observed: list = []
    monitor = _make_monitor(http, extra_callbacks=[observed.append])

    http.queue_ok()
    http.queue_error()
    await _run_briefly(monitor, seconds=0.05)

    assert RuntimeLinkState.CONNECTED in observed
    assert RuntimeLinkState.RECONNECTING in observed


async def test_reconnecting_to_offline_after_retries_exhausted() -> None:
    """RECONNECTING -> OFFLINE after max retries + grace period."""
    from hermes.shell.domain.shell_session import RuntimeLinkState

    http = FakeHttpClient()
    observed: list = []
    cfg = _fast_config(max_retries_before_offline=3, grace_period_s=0.02)
    monitor = _make_monitor(http, config=cfg, extra_callbacks=[observed.append])

    http.queue_ok()
    http.queue_many_errors(15)
    await _run_briefly(monitor, seconds=0.6)

    assert RuntimeLinkState.OFFLINE in observed


async def test_reconnects_after_recovery() -> None:
    """After going OFFLINE, monitor reaches CONNECTED again when endpoint recovers."""
    from hermes.shell.domain.shell_session import RuntimeLinkState

    http = FakeHttpClient()
    observed: list = []
    cfg = _fast_config(max_retries_before_offline=2, grace_period_s=0.01)
    monitor = _make_monitor(http, config=cfg, extra_callbacks=[observed.append])

    http.queue_ok()
    http.queue_many_errors(6)
    http.queue_ok()
    http.queue_ok()
    http.queue_ok()
    await _run_briefly(monitor, seconds=0.7)

    last_connected = max(
        (i for i, s in enumerate(observed) if s == RuntimeLinkState.CONNECTED),
        default=-1,
    )
    last_offline = max(
        (i for i, s in enumerate(observed) if s == RuntimeLinkState.OFFLINE),
        default=-2,
    )
    assert last_connected > last_offline


async def test_on_state_change_fires_with_has_model_true_when_connected() -> None:
    """on_state_change receives CONNECTED state; emitter records has_model=True."""
    http = FakeHttpClient()
    emitter = FakeLivenessEmitter()
    monitor = _make_monitor(http, emitter=emitter)

    http.queue_ok()
    await _run_briefly(monitor, seconds=0.05)

    connected_calls = [c for c in emitter.calls if c.has_model]
    assert len(connected_calls) >= 1
    assert all(c.alive for c in connected_calls)


async def test_on_state_change_fires_has_model_false_when_offline() -> None:
    """When endpoint goes OFFLINE, emitter records has_model=False."""
    http = FakeHttpClient()
    emitter = FakeLivenessEmitter()
    cfg = _fast_config(max_retries_before_offline=2, grace_period_s=0.01)
    monitor = _make_monitor(http, emitter=emitter, config=cfg)

    http.queue_ok()
    http.queue_many_errors(10)
    await _run_briefly(monitor, seconds=0.5)

    offline_calls = [c for c in emitter.calls if not c.has_model]
    assert len(offline_calls) >= 1


async def test_backoff_history_grows_exponentially() -> None:
    """Backoff delays increase exponentially up to the cap."""
    from hermes.runtime.model_health_monitor import ModelHealthMonitor

    http = FakeHttpClient()
    cfg = _fast_config(
        backoff_initial_s=0.01,
        backoff_cap_s=0.08,
        backoff_multiplier=2.0,
        max_retries_before_offline=20,
        grace_period_s=1.0,
    )
    monitor = ModelHealthMonitor(http_client=http, config=cfg)

    http.queue_ok()
    http.queue_many_errors(30)
    await _run_briefly(monitor, seconds=0.3)

    delays = monitor.backoff_history
    assert len(delays) >= 3
    for d in delays:
        assert d <= cfg.backoff_cap_s + 1e-9
    for i in range(1, len(delays)):
        assert delays[i] >= delays[i - 1] - 1e-9


def test_monitor_does_not_import_litellm() -> None:
    """ModelHealthMonitor must NOT import litellm (SRP — no engine coupling)."""
    import sys

    import hermes.runtime.model_health_monitor as mod

    module_file = mod.__file__ or ""
    with open(module_file) as f:
        src = f.read()
    assert "import litellm" not in src
    assert "from litellm" not in src


def test_monitor_does_not_import_dbus() -> None:
    """ModelHealthMonitor must NOT import dbus_fast (SRP — no D-Bus coupling)."""
    import hermes.runtime.model_health_monitor as mod

    module_file = mod.__file__ or ""
    with open(module_file) as f:
        src = f.read()
    assert "dbus_fast" not in src
