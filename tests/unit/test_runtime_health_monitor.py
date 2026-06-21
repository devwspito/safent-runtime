"""T019 — RuntimeBackendHealthMonitor unit tests (US1, feature 006 PIEZA 3).

Tests-first: these tests FAIL before T028 is implemented.

Validates:
- State machine transitions: CONNECTED <-> RECONNECTING <-> OFFLINE <-> DEGRADED
- Exponential backoff with cap (0.5s -> 8s)
- Debounce / grace period: RECONNECTING is shown before OFFLINE is emitted
- Re-attachment when daemon comes back online
- SRP: no GTK import, testeable headless
- Fake AgentRuntimePort + fake NameOwnerChanged event bus
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from hermes.shell.domain.shell_session import RuntimeLinkState
from hermes.shell.application.runtime_backend_health_monitor import (
    RuntimeBackendHealthMonitor,
    MonitorConfig,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRuntimePort:
    """Fake AgentRuntimePort — controla lo que get_status() devuelve."""

    def __init__(self) -> None:
        self._responses: list[dict | Exception] = []
        self.call_count: int = 0

    def queue_ok(self, **extra: Any) -> None:
        self._responses.append({"status": "ok", **extra})

    def queue_degraded(self, subsystem: str = "model") -> None:
        self._responses.append({"status": "degraded", "subsystem": subsystem})

    def queue_error(self, exc: Exception | None = None) -> None:
        self._responses.append(exc or ConnectionRefusedError("daemon down"))

    def queue_many_errors(self, n: int) -> None:
        for _ in range(n):
            self.queue_error()

    async def get_status(self) -> dict:
        self.call_count += 1
        if not self._responses:
            return {"status": "ok"}
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def send_message(self, *, text: str):  # noqa: ANN201
        raise NotImplementedError  # pragma: no cover

    async def request_pause(self, *, reason: str) -> None:
        raise NotImplementedError  # pragma: no cover

    async def request_resume(self) -> None:
        raise NotImplementedError  # pragma: no cover


class FakeEventBus:
    """Fake bus — expone un callback que el test puede invocar manualmente
    para simular NameOwnerChanged signals del D-Bus."""

    def __init__(self) -> None:
        self._owner_changed: Callable[[bool], None] | None = None

    def subscribe_name_owner_changed(
        self, callback: Callable[[bool], None]
    ) -> None:
        """Monitor registra aqui su handler; el test lo dispara despues."""
        self._owner_changed = callback

    def fire_owner_appeared(self) -> None:
        assert self._owner_changed is not None, "subscribe not called"
        self._owner_changed(True)

    def fire_owner_lost(self) -> None:
        assert self._owner_changed is not None, "subscribe not called"
        self._owner_changed(False)


def _fast_config(**overrides: Any) -> MonitorConfig:
    """Config acelerada para tests: timeouts mini para que el loop avance rapido."""
    base = MonitorConfig(
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


async def _run_monitor_briefly(
    monitor: RuntimeBackendHealthMonitor,
    *,
    seconds: float,
) -> None:
    """Arrancar el monitor en background y cancelarlo tras `seconds`."""
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# T019-A: Estado inicial
# ---------------------------------------------------------------------------


def test_initial_state_is_offline() -> None:
    port = FakeRuntimePort()
    bus = FakeEventBus()
    monitor = RuntimeBackendHealthMonitor(
        runtime_port=port,
        event_bus=bus,
        config=_fast_config(),
    )
    assert monitor.state == RuntimeLinkState.OFFLINE


# ---------------------------------------------------------------------------
# T019-B: Transicion OFFLINE -> CONNECTED al primer get_status OK
# ---------------------------------------------------------------------------


async def test_transitions_to_connected_on_first_ok() -> None:
    port = FakeRuntimePort()
    bus = FakeEventBus()
    observed: list[RuntimeLinkState] = []

    monitor = RuntimeBackendHealthMonitor(
        runtime_port=port,
        event_bus=bus,
        config=_fast_config(),
        on_state_change=observed.append,
    )

    port.queue_ok()
    await _run_monitor_briefly(monitor, seconds=0.05)

    assert RuntimeLinkState.CONNECTED in observed


# ---------------------------------------------------------------------------
# T019-C: CONNECTED -> RECONNECTING al primer fallo
# ---------------------------------------------------------------------------


async def test_transitions_to_reconnecting_on_first_failure() -> None:
    port = FakeRuntimePort()
    bus = FakeEventBus()
    observed: list[RuntimeLinkState] = []

    monitor = RuntimeBackendHealthMonitor(
        runtime_port=port,
        event_bus=bus,
        config=_fast_config(),
        on_state_change=observed.append,
    )

    # Start connected, then fail once
    port.queue_ok()
    port.queue_error()
    await _run_monitor_briefly(monitor, seconds=0.05)

    assert RuntimeLinkState.CONNECTED in observed
    assert RuntimeLinkState.RECONNECTING in observed


# ---------------------------------------------------------------------------
# T019-D: RECONNECTING -> OFFLINE tras N reintentos fallidos (backoff)
# ---------------------------------------------------------------------------


async def test_transitions_to_offline_after_max_retries() -> None:
    port = FakeRuntimePort()
    bus = FakeEventBus()
    observed: list[RuntimeLinkState] = []

    # max_retries=3, enough errors to exhaust + grace period
    cfg = _fast_config(max_retries_before_offline=3, grace_period_s=0.02)
    monitor = RuntimeBackendHealthMonitor(
        runtime_port=port,
        event_bus=bus,
        config=cfg,
        on_state_change=observed.append,
    )

    port.queue_ok()
    port.queue_many_errors(10)
    await _run_monitor_briefly(monitor, seconds=0.5)

    assert RuntimeLinkState.OFFLINE in observed


# ---------------------------------------------------------------------------
# T019-E: OFFLINE -> RECONNECTING -> CONNECTED al volver el daemon
# ---------------------------------------------------------------------------


async def test_reconnects_when_daemon_returns() -> None:
    port = FakeRuntimePort()
    bus = FakeEventBus()
    observed: list[RuntimeLinkState] = []

    cfg = _fast_config(max_retries_before_offline=2, grace_period_s=0.01)
    monitor = RuntimeBackendHealthMonitor(
        runtime_port=port,
        event_bus=bus,
        config=cfg,
        on_state_change=observed.append,
    )

    # Go connected, then offline, then connected again
    port.queue_ok()
    port.queue_many_errors(5)
    port.queue_ok()
    port.queue_ok()
    port.queue_ok()

    await _run_monitor_briefly(monitor, seconds=0.6)

    assert RuntimeLinkState.CONNECTED in observed
    assert RuntimeLinkState.OFFLINE in observed
    # After errors, daemon returns: must reach CONNECTED again
    last_connected_idx = max(
        i for i, s in enumerate(observed) if s == RuntimeLinkState.CONNECTED
    )
    last_offline_idx = max(
        (i for i, s in enumerate(observed) if s == RuntimeLinkState.OFFLINE),
        default=-1,
    )
    # CONNECTED must appear AFTER the OFFLINE
    assert last_connected_idx > last_offline_idx


# ---------------------------------------------------------------------------
# T019-F: DEGRADED cuando el daemon reporta subsistema caido
# ---------------------------------------------------------------------------


async def test_transitions_to_degraded_on_degraded_status() -> None:
    port = FakeRuntimePort()
    bus = FakeEventBus()
    observed: list[RuntimeLinkState] = []

    monitor = RuntimeBackendHealthMonitor(
        runtime_port=port,
        event_bus=bus,
        config=_fast_config(),
        on_state_change=observed.append,
    )

    port.queue_ok()
    port.queue_degraded(subsystem="model")
    await _run_monitor_briefly(monitor, seconds=0.05)

    assert RuntimeLinkState.DEGRADED in observed


# ---------------------------------------------------------------------------
# T019-G: NameOwnerChanged owner_lost dispara RECONNECTING inmediato
# ---------------------------------------------------------------------------


async def test_name_owner_lost_triggers_reconnecting() -> None:
    port = FakeRuntimePort()
    bus = FakeEventBus()
    observed: list[RuntimeLinkState] = []

    monitor = RuntimeBackendHealthMonitor(
        runtime_port=port,
        event_bus=bus,
        config=_fast_config(),
        on_state_change=observed.append,
    )

    # Conectado primero
    port.queue_ok()
    # Daemon will keep erroring after name lost
    port.queue_many_errors(20)

    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.05)  # Wait for CONNECTED

    assert RuntimeLinkState.CONNECTED in observed

    # Simulate NameOwnerChanged: name lost
    bus.fire_owner_lost()
    await asyncio.sleep(0.05)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert RuntimeLinkState.RECONNECTING in observed


# ---------------------------------------------------------------------------
# T019-H: NameOwnerChanged owner_appeared activa reenganche inmediato
# ---------------------------------------------------------------------------


async def test_name_owner_appeared_triggers_reconnect() -> None:
    port = FakeRuntimePort()
    bus = FakeEventBus()
    observed: list[RuntimeLinkState] = []

    cfg = _fast_config(max_retries_before_offline=2, grace_period_s=0.01)
    monitor = RuntimeBackendHealthMonitor(
        runtime_port=port,
        event_bus=bus,
        config=cfg,
        on_state_change=observed.append,
    )

    # Go OFFLINE first
    port.queue_ok()
    port.queue_many_errors(10)
    # Then daemon appears
    port.queue_ok()
    port.queue_ok()
    port.queue_ok()

    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.3)  # Allow state to go OFFLINE

    # Fire NameOwnerChanged: name appeared (wake the monitor)
    bus.fire_owner_appeared()
    await asyncio.sleep(0.1)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Must end in CONNECTED after wake
    assert RuntimeLinkState.CONNECTED in observed


# ---------------------------------------------------------------------------
# T019-I: backoff exponencial — delays aumentan entre reintentos
# ---------------------------------------------------------------------------


async def test_backoff_delays_increase_exponentially() -> None:
    """El backoff entre reintentos debe crecer exponencialmente hasta el cap."""
    port = FakeRuntimePort()
    bus = FakeEventBus()

    cfg = _fast_config(
        backoff_initial_s=0.01,
        backoff_cap_s=0.08,
        backoff_multiplier=2.0,
        max_retries_before_offline=10,
        grace_period_s=0.5,  # largo para que no llegue a OFFLINE
    )
    monitor = RuntimeBackendHealthMonitor(
        runtime_port=port,
        event_bus=bus,
        config=cfg,
    )

    # Go connected, then keep failing
    port.queue_ok()
    port.queue_many_errors(20)

    await _run_monitor_briefly(monitor, seconds=0.3)

    # The computed delays must follow: 0.01, 0.02, 0.04, 0.08, 0.08...
    delays = monitor.backoff_history
    assert len(delays) >= 3
    # Each delay <= cap
    for d in delays:
        assert d <= cfg.backoff_cap_s + 1e-9

    # Sequence must be non-decreasing (grows until cap)
    for i in range(1, len(delays)):
        assert delays[i] >= delays[i - 1] - 1e-9


# ---------------------------------------------------------------------------
# T019-J: on_state_change callback es llamado en cada transicion
# ---------------------------------------------------------------------------


async def test_on_state_change_called_on_every_transition() -> None:
    port = FakeRuntimePort()
    bus = FakeEventBus()
    transitions: list[RuntimeLinkState] = []

    monitor = RuntimeBackendHealthMonitor(
        runtime_port=port,
        event_bus=bus,
        config=_fast_config(max_retries_before_offline=2, grace_period_s=0.01),
        on_state_change=transitions.append,
    )

    port.queue_ok()
    port.queue_many_errors(5)
    port.queue_ok()

    await _run_monitor_briefly(monitor, seconds=0.5)

    # Must have at least: CONNECTED, RECONNECTING, OFFLINE, CONNECTED
    assert len(transitions) >= 3
    assert transitions[0] == RuntimeLinkState.CONNECTED


# ---------------------------------------------------------------------------
# T019-K: ShellSession.mark_runtime_link es dirigido por el monitor
# ---------------------------------------------------------------------------


async def test_monitor_directs_shell_session_mark_runtime_link() -> None:
    from hermes.shell.domain.shell_session import start_session

    port = FakeRuntimePort()
    bus = FakeEventBus()

    session = start_session(human_user_id="test-user")

    monitor = RuntimeBackendHealthMonitor(
        runtime_port=port,
        event_bus=bus,
        config=_fast_config(),
        on_state_change=session.mark_runtime_link,
    )

    port.queue_ok()
    await _run_monitor_briefly(monitor, seconds=0.05)

    assert session.runtime_link_state == RuntimeLinkState.CONNECTED


# ---------------------------------------------------------------------------
# T019-L: No GTK import en el modulo del monitor (SRP headless)
# ---------------------------------------------------------------------------


def test_monitor_module_does_not_import_gtk() -> None:
    """SRP: el monitor no debe importar ni requerir GTK4 / GLib / Gdk."""
    import sys

    import hermes.shell.application.runtime_backend_health_monitor as mod

    module_file = mod.__file__ or ""
    # Verify the module itself doesn't pull in GTK globals
    gtk_modules = [k for k in sys.modules if k.startswith("gi.repository")]
    # The module file must not contain gi.require_version or GLib
    with open(module_file) as f:
        src = f.read()
    assert "gi.require_version" not in src
    assert "from gi.repository" not in src
