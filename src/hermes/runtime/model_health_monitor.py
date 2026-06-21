"""ModelHealthMonitor — P3, feature 008, US2 (SC-3/SC-4).

Monitors the local LLM inference endpoint (hermes-llm.service) and drives
AgentLivenessChanged(alive, has_model) via the D-Bus adapter callback.

Design mirrors RuntimeBackendHealthMonitor (feature 006, PIEZA 3) intentionally:
  - Same state machine: OFFLINE/CONNECTED/RECONNECTING/DEGRADED.
  - Same backoff + grace-period semantics.
  - Pure asyncio, no threads, no blocking calls.
  - No GTK, no D-Bus import (those belong in infrastructure/presentation).
  - Dependency-injected HTTP client port (testable without real network).

The _tick() method calls http_client.is_healthy() instead of a D-Bus port.
State transitions drive on_state_change(RuntimeLinkState), which the caller
wires to emit_liveness_changed(alive=True, has_model=<state==CONNECTED>).

Configuration:
  Endpoint base URL is read from HERMES_MODEL_BASE_URL (default localhost:8000).
  The default HttpModelClient calls GET /v1/models; a 200 with a non-empty
  model list signals healthy. Any error or empty list signals unhealthy.

Run as an asyncio task in the daemon gather:
    monitor = ModelHealthMonitor(
        http_client=HttpModelClient.from_env(),
        on_state_change=lambda s: adapter.emit_liveness_changed(
            alive=True,
            has_model=(s == RuntimeLinkState.CONNECTED),
        ),
    )
    asyncio.create_task(monitor.run())
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hermes.shell.domain.shell_session import RuntimeLinkState

logger = logging.getLogger("hermes.runtime.model_health_monitor")


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------


@runtime_checkable
class ModelEndpointClient(Protocol):
    """Port: abstracts the HTTP check for local LLM health.

    Production adapter: HttpModelClient (calls GET /v1/models).
    Test adapter: FakeHttpClient (controls responses programmatically).
    """

    async def is_healthy(self) -> bool:
        """Returns True if the endpoint is reachable and has at least one model.

        Raises on hard network failure (connection refused, timeout).
        Returns False for valid but empty responses (no models loaded yet).
        """
        ...


# ---------------------------------------------------------------------------
# Configuration value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelMonitorConfig:
    """Tunable knobs for ModelHealthMonitor.

    Defaults match the spec (feature 008, SC-3/SC-4):
    - poll_interval_s: healthy-state polling cadence (5s — slower than D-Bus)
    - backoff_initial_s: first retry delay (1s)
    - backoff_cap_s: maximum retry interval (30s)
    - backoff_multiplier: exponential factor
    - grace_period_s: minimum time in RECONNECTING before OFFLINE (30s covers
      vLLM reload time on a weight swap)
    - max_retries_before_offline: consecutive failures to exhaust grace
    """

    poll_interval_s: float = 30.0  # ARM idle: 30 s settled (was 5 s — 6× wakeup reduction)
    backoff_initial_s: float = 1.0
    backoff_cap_s: float = 30.0
    backoff_multiplier: float = 2.0
    grace_period_s: float = 30.0
    max_retries_before_offline: int = 10


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class ModelHealthMonitor:
    """Polls the local LLM endpoint and emits state changes.

    Mirrors RuntimeBackendHealthMonitor API so the two monitors are composable
    without surprises. The caller wires on_state_change to the D-Bus adapter.
    """

    def __init__(
        self,
        *,
        http_client: ModelEndpointClient,
        config: ModelMonitorConfig | None = None,
        on_state_change: Callable[[RuntimeLinkState], None] | None = None,
    ) -> None:
        self._client = http_client
        self._config = config or ModelMonitorConfig()
        self._on_state_change = on_state_change or (lambda _s: None)

        self._state = RuntimeLinkState.OFFLINE
        self._consecutive_failures: int = 0
        self._backoff_current: float = self._config.backoff_initial_s
        self._grace_started_at: float | None = None
        self._backoff_history: list[float] = []

    # ------------------------------------------------------------------
    # Public API (mirrors RuntimeBackendHealthMonitor)
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
        logger.info("hermes.model_health_monitor.started")
        while True:
            await self._tick()
            await self._sleep_until_next_poll()

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        try:
            healthy = await self._client.is_healthy()
        except Exception:
            self._handle_failure()
            return

        if healthy:
            self._handle_ok()
        else:
            self._handle_failure()

    def _handle_ok(self) -> None:
        self._grace_started_at = None
        self._consecutive_failures = 0
        self._reset_backoff()
        self._transition_to(RuntimeLinkState.CONNECTED)

    def _handle_failure(self) -> None:
        self._consecutive_failures += 1
        loop_time = asyncio.get_event_loop().time()

        if self._state == RuntimeLinkState.CONNECTED:
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
            elif self._state != RuntimeLinkState.RECONNECTING:
                self._transition_to(RuntimeLinkState.RECONNECTING)

    def _grace_elapsed(self, now: float) -> float:
        if self._grace_started_at is None:
            return self._config.grace_period_s
        return now - self._grace_started_at

    async def _sleep_until_next_poll(self) -> None:
        if self._state == RuntimeLinkState.CONNECTED:
            delay = self._config.poll_interval_s
        else:
            delay = self._next_backoff()

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.sleep(delay), timeout=delay + 1)

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
            "hermes.model_health_monitor.state_change: %s -> %s  "
            "(consecutive_failures=%d)",
            self._state,
            new_state,
            self._consecutive_failures,
        )
        self._state = new_state
        self._on_state_change(new_state)


# ---------------------------------------------------------------------------
# Production HTTP client
# ---------------------------------------------------------------------------


class HttpModelClient:
    """Calls GET /v1/models on the local inference endpoint.

    Healthy = 200 response with at least one model in the list.
    Any other outcome (timeout, connection error, empty list) = unhealthy.

    Uses aiohttp lazily so the module is importable without it installed
    (tests inject a fake instead).

    The aiohttp ClientSession is reused across polls (created on first call,
    closed on explicit close()). Creating a new session per poll was causing
    unnecessary connection churn and GC pressure on ARM/low-RAM devices.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8000",
        timeout_s: float = 10.0,
    ) -> None:
        self._models_url = base_url.rstrip("/") + "/v1/models"
        self._timeout_s = timeout_s
        self._session: "aiohttp.ClientSession | None" = None  # type: ignore[name-defined]

    @classmethod
    def from_env(cls) -> HttpModelClient:
        """Builds from HERMES_MODEL_BASE_URL env var (default localhost:8000)."""
        import os  # noqa: PLC0415

        base_url = os.environ.get(
            "HERMES_MODEL_BASE_URL", "http://127.0.0.1:8000"
        ).rstrip("/")
        return cls(base_url=base_url)

    async def _get_session(self) -> "aiohttp.ClientSession":  # type: ignore[name-defined]
        """Return the shared session, creating it on first call."""
        import aiohttp  # noqa: PLC0415

        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._timeout_s)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        """Close the persistent session. Call when the monitor shuts down."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def is_healthy(self) -> bool:
        """Returns True if /v1/models responds with at least one model."""
        from http import HTTPStatus  # noqa: PLC0415

        session = await self._get_session()
        async with session.get(self._models_url) as resp:
            if resp.status != HTTPStatus.OK:
                return False
            body = await resp.json()
            models = body.get("data", []) if isinstance(body, dict) else []
            return len(models) > 0
