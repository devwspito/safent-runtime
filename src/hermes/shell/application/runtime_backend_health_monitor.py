"""RuntimeBackendHealthMonitor — feature 006, PIEZA 3, US1 (T028).

Application layer of hermes/shell. Monitors hermes-runtime.service
health and drives ShellSession.mark_runtime_link(...).

Design contracts:
- NO GTK / GLib / gi.repository anywhere in this module (SRP, headless testable).
- Pure asyncio: no threads, no blocking calls.
- Depends on AgentRuntimePort (poll) + a NameOwnerChangedBus (signal).
- Emits state changes via on_state_change callback (decoupled from GTK).

State machine (RuntimeLinkState):
  OFFLINE -(ok)-> CONNECTED -(error)-> RECONNECTING -(N retries)-> OFFLINE
  CONNECTED -(degraded status)-> DEGRADED -(ok)-> CONNECTED
  RECONNECTING -(ok)-> CONNECTED
  * NameOwnerChanged(lost)  -> immediate RECONNECTING from CONNECTED
  * NameOwnerChanged(appeared) -> wake the poll loop (shorten wait)

Backoff: exponential starting at backoff_initial_s, capped at backoff_cap_s.
Grace period: RECONNECTING is displayed for at least grace_period_s before
  transitioning to OFFLINE (covers daemon restart window, FR-006 Edge Case).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from hermes.shell.domain.shell_session import RuntimeLinkState

logger = logging.getLogger("hermes.shell.application.health_monitor")


# ---------------------------------------------------------------------------
# Ports (defined here — adapters live in infrastructure)
# ---------------------------------------------------------------------------


@runtime_checkable
class NameOwnerChangedBus(Protocol):
    """Port for D-Bus NameOwnerChanged signals on org.hermes.Runtime1.

    The infrastructure adapter subscribes to the real D-Bus signal.
    Tests inject a fake that fires the callback manually.
    """

    def subscribe_name_owner_changed(
        self, callback: Callable[[bool], None]
    ) -> None:
        """Register `callback(name_has_owner: bool)`.

        Called with True when org.hermes.Runtime1 appears,
        False when it disappears.
        """
        ...


# ---------------------------------------------------------------------------
# Configuration value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorConfig:
    """Tunable parameters for RuntimeBackendHealthMonitor.

    All times are in seconds. Defaults match the spec (research.md PIEZA 3):
    - poll_interval_s: normal healthy-state polling cadence (~2s)
    - backoff_initial_s: first retry delay (0.5s in prod, tiny in tests)
    - backoff_cap_s: maximum retry interval (8s in prod)
    - backoff_multiplier: exponential growth factor
    - grace_period_s: min time in RECONNECTING before promoting to OFFLINE
      (covers RestartSec*2 + margin ≈ 6s in prod)
    - max_retries_before_offline: consecutive failures that exhaust grace
    """

    poll_interval_s: float = 15.0  # ARM idle: 15 s settled (was 2 s — 7× wakeup reduction)
    backoff_initial_s: float = 0.5
    backoff_cap_s: float = 8.0
    backoff_multiplier: float = 2.0
    grace_period_s: float = 6.0
    max_retries_before_offline: int = 5


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class RuntimeBackendHealthMonitor:
    """Monitors the hermes-runtime.service link and drives state transitions.

    Usage:
        monitor = RuntimeBackendHealthMonitor(
            runtime_port=dbus_adapter,
            event_bus=dbus_name_owner_bus,
            on_state_change=session.mark_runtime_link,
        )
        asyncio.create_task(monitor.run())
    """

    def __init__(
        self,
        *,
        runtime_port: Any,  # AgentRuntimePort — avoid circular import
        event_bus: NameOwnerChangedBus,
        config: MonitorConfig | None = None,
        on_state_change: Callable[[RuntimeLinkState], None] | None = None,
    ) -> None:
        self._port = runtime_port
        self._config = config or MonitorConfig()
        self._on_state_change = on_state_change or (lambda _s: None)

        self._state = RuntimeLinkState.OFFLINE
        self._consecutive_failures: int = 0
        self._backoff_current: float = self._config.backoff_initial_s
        self._grace_started_at: float | None = None

        # backoff_history tracks the delays applied — exposed for test assertions
        self._backoff_history: list[float] = []

        # asyncio.Event: fired on NameOwnerChanged to wake the poll sooner
        self._wake_event: asyncio.Event = asyncio.Event()

        event_bus.subscribe_name_owner_changed(self._on_name_owner_changed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> RuntimeLinkState:
        return self._state

    @property
    def backoff_history(self) -> list[float]:
        """Delays applied during backoff — for test introspection."""
        return list(self._backoff_history)

    async def run(self) -> None:
        """Async loop — run as a background task. Cancellable."""
        logger.info("health monitor started")
        while True:
            await self._tick()
            await self._sleep_until_next_poll()

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        try:
            status = await self._port.get_status()
            self._handle_ok_response(status)
        except Exception:
            self._handle_failure()

    def _handle_ok_response(self, status: dict) -> None:
        self._grace_started_at = None
        raw = status.get("status", "ok")

        if raw == "degraded":
            self._consecutive_failures = 0
            self._reset_backoff()
            self._transition_to(RuntimeLinkState.DEGRADED)
            return

        self._consecutive_failures = 0
        self._reset_backoff()
        self._transition_to(RuntimeLinkState.CONNECTED)

    def _handle_failure(self) -> None:
        self._consecutive_failures += 1
        loop_time = asyncio.get_event_loop().time()

        if self._state == RuntimeLinkState.CONNECTED:
            # First failure from CONNECTED: enter RECONNECTING, start grace
            self._grace_started_at = loop_time
            self._transition_to(RuntimeLinkState.RECONNECTING)
            return

        if self._state in (RuntimeLinkState.RECONNECTING, RuntimeLinkState.OFFLINE):
            grace_elapsed = self._grace_elapsed(loop_time)
            retries_exhausted = (
                self._consecutive_failures >= self._config.max_retries_before_offline
            )
            grace_expired = grace_elapsed >= self._config.grace_period_s

            if retries_exhausted and grace_expired:
                self._transition_to(RuntimeLinkState.OFFLINE)
            else:
                # Keep in RECONNECTING during grace window
                if self._state != RuntimeLinkState.RECONNECTING:
                    self._transition_to(RuntimeLinkState.RECONNECTING)

        # DEGRADED with errors -> RECONNECTING
        if self._state == RuntimeLinkState.DEGRADED:
            self._grace_started_at = loop_time
            self._transition_to(RuntimeLinkState.RECONNECTING)

    def _grace_elapsed(self, now: float) -> float:
        if self._grace_started_at is None:
            return self._config.grace_period_s  # treat as expired
        return now - self._grace_started_at

    async def _sleep_until_next_poll(self) -> None:
        if self._state == RuntimeLinkState.CONNECTED:
            delay = self._config.poll_interval_s
        else:
            delay = self._next_backoff()

        self._wake_event.clear()
        try:
            await asyncio.wait_for(
                self._wake_event.wait(),
                timeout=delay,
            )
            # Woken early by NameOwnerChanged — poll immediately
        except asyncio.TimeoutError:
            pass

    def _next_backoff(self) -> float:
        delay = min(self._backoff_current, self._config.backoff_cap_s)
        self._backoff_history.append(delay)
        self._backoff_current = min(
            self._backoff_current * self._config.backoff_multiplier,
            self._config.backoff_cap_s,
        )
        return delay

    def _reset_backoff(self) -> None:
        self._backoff_current = self._config.backoff_initial_s

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _transition_to(self, new_state: RuntimeLinkState) -> None:
        if new_state == self._state:
            return
        logger.info(
            "runtime link state: %s -> %s  (consecutive_failures=%d)",
            self._state,
            new_state,
            self._consecutive_failures,
        )
        self._state = new_state
        self._on_state_change(new_state)

    # ------------------------------------------------------------------
    # NameOwnerChanged handler (called from event bus subscription)
    # ------------------------------------------------------------------

    def _on_name_owner_changed(self, name_has_owner: bool) -> None:
        if name_has_owner:
            logger.info("NameOwnerChanged: org.hermes.Runtime1 appeared — waking poll")
            self._wake_event.set()
        else:
            logger.warning("NameOwnerChanged: org.hermes.Runtime1 lost — entering RECONNECTING")
            if self._state == RuntimeLinkState.CONNECTED:
                self._grace_started_at = asyncio.get_event_loop().time()
                self._transition_to(RuntimeLinkState.RECONNECTING)
            self._wake_event.set()
