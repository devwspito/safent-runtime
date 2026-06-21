"""BrowserAdmissionGuard — RAM-safety layer for concurrent browser sessions.

Phase 2a (feature 006 / PIEZA 4b): Each agent-browser --session is a separate
Chromium-ish process (~hundreds of MB). On unified-memory hosts (DGX) browser
RAM competes with LLM KV cache. This guard caps concurrent sessions against
POST-model-load MemAvailable so the node never OOM.

Design:
  - MemoryReaderPort: Protocol with mem_available_mb() -> int | None.
    None means unreadable (non-Linux, test env) — never raises.
  - ProcMeminfoReader: reads /proc/meminfo MemAvailable (kB→MB).
  - BrowserAdmissionGuard:
      * Capacity = clamp(explicit_override or floor(mem/session_mb), 1, hard_cap)
      * If mem unreadable at boot, falls back to a safe small value (4 sessions).
      * acquire(context_id): asyncio.Semaphore + dynamic memory guard (backpressure,
        not reject) + shutdown abort.
      * release(context_id): idempotent — releasing an unknown context_id is no-op.
      * Tracks acquired context_ids in an explicit set (no Semaphore._value reading).

Architectural principle: admission lives in the execution layer daemon, NOT in
any HTTP API. This is an OS resource manager, not a web gate.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Protocol, runtime_checkable

logger = logging.getLogger("hermes.execution.browser_admission")

# Environment variable names
_ENV_SESSION_MB = "HERMES_BROWSER_SESSION_MB"
_ENV_MAX_SESSIONS = "HERMES_BROWSER_MAX_SESSIONS"
_ENV_HARD_CAP = "HERMES_BROWSER_HARD_CAP"
_ENV_HEADROOM_MB = "HERMES_BROWSER_HEADROOM_MB"

# Defaults
_DEFAULT_SESSION_MB = 400
_DEFAULT_HARD_CAP = 64
_DEFAULT_HEADROOM_MB = 512
_DEFAULT_FALLBACK_SESSIONS = 4
_DEFAULT_RECHECK_INTERVAL_S = 0.5


class BrowserAdmissionDenied(RuntimeError):
    """Raised when shutdown is requested while waiting for a browser permit."""


@runtime_checkable
class MemoryReaderPort(Protocol):
    """Port for reading available system memory. Returns None if unreadable."""

    def mem_available_mb(self) -> int | None:
        """Return MemAvailable in MiB, or None if unreadable (never raises)."""
        ...


class ProcMeminfoReader:
    """Reads MemAvailable from /proc/meminfo (Linux only).

    Returns None on any error — non-Linux, permission denied, parse failures.
    Never raises.
    """

    def mem_available_mb(self) -> int | None:
        try:
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        kb = int(line.split()[1])
                        return kb // 1024
        except (OSError, ValueError, IndexError):
            pass
        return None


def _compute_capacity(
    *,
    mem_available_mb: int | None,
    session_mb: int,
    hard_cap: int,
    explicit_override: int | None,
    fallback: int,
) -> int:
    """Pure capacity computation — separated for testability.

    Priority:
    1. explicit_override (HERMES_BROWSER_MAX_SESSIONS) → clamp to [1, hard_cap].
    2. floor(mem_available_mb / session_mb) if mem is readable.
    3. fallback if mem is unreadable.
    Always clamped to [1, hard_cap].
    """
    if explicit_override is not None:
        raw = explicit_override
    elif mem_available_mb is not None:
        raw = math.floor(mem_available_mb / session_mb) if session_mb > 0 else 1
    else:
        raw = fallback
    return max(1, min(raw, hard_cap))


class BrowserAdmissionGuard:
    """Admission controller for browser sessions against available RAM.

    Shared across all workers (one instance per daemon). Pure asyncio — no threads.

    Acquire/release invariant:
      - acquire: called on BROWSER/DESKTOP_APP open, before OS process spawn.
      - release: called on BROWSER/DESKTOP_APP close (or open-failure rollback).
      - Exactly one acquire and exactly one release per admitted context_id.
    """

    def __init__(
        self,
        *,
        memory_reader: MemoryReaderPort | None = None,
        recheck_interval_s: float = _DEFAULT_RECHECK_INTERVAL_S,
    ) -> None:
        self._reader = memory_reader if memory_reader is not None else ProcMeminfoReader()
        self._recheck_interval_s = recheck_interval_s

        self._session_mb = int(os.environ.get(_ENV_SESSION_MB, str(_DEFAULT_SESSION_MB)))
        self._hard_cap = int(os.environ.get(_ENV_HARD_CAP, str(_DEFAULT_HARD_CAP)))
        self._headroom_mb = int(os.environ.get(_ENV_HEADROOM_MB, str(_DEFAULT_HEADROOM_MB)))

        explicit_override = _parse_optional_int(os.environ.get(_ENV_MAX_SESSIONS))
        mem_now = self._reader.mem_available_mb()

        if mem_now is None:
            logger.warning(
                "hermes.execution.browser_admission.meminfo_unreadable — "
                "falling back to %d concurrent browser sessions",
                _DEFAULT_FALLBACK_SESSIONS,
            )

        self._capacity = _compute_capacity(
            mem_available_mb=mem_now,
            session_mb=self._session_mb,
            hard_cap=self._hard_cap,
            explicit_override=explicit_override,
            fallback=_DEFAULT_FALLBACK_SESSIONS,
        )
        self._sem = asyncio.Semaphore(self._capacity)
        self._acquired_ids: set[str] = set()
        self._shutdown_event: asyncio.Event = asyncio.Event()

        logger.info(
            "hermes.execution.browser_admission.ready "
            "capacity=%d session_mb=%d headroom_mb=%d hard_cap=%d",
            self._capacity,
            self._session_mb,
            self._headroom_mb,
            self._hard_cap,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self, context_id: str) -> None:
        """Acquire a browser permit for context_id.

        Backpressure: parks while MemAvailable < session_mb + headroom_mb even
        if the semaphore has capacity. Does NOT reject — waits for recovery.
        Aborts with BrowserAdmissionDenied if shutdown is signaled.

        Shutdown is checked both while waiting for the semaphore and during the
        dynamic memory guard loop.
        """
        await self._acquire_semaphore(context_id)
        self._acquired_ids.add(context_id)

        await self._dynamic_memory_guard(context_id)

        logger.debug(
            "hermes.execution.browser_admission.acquired context_id=%s active=%d",
            context_id,
            len(self._acquired_ids),
        )

    def release(self, context_id: str) -> None:
        """Release the permit for context_id. Idempotent for unknown context_ids."""
        if context_id not in self._acquired_ids:
            logger.debug(
                "hermes.execution.browser_admission.release_noop context_id=%s "
                "(not in acquired set — idempotent)",
                context_id,
            )
            return

        self._acquired_ids.discard(context_id)
        self._sem.release()

        logger.debug(
            "hermes.execution.browser_admission.released context_id=%s active=%d",
            context_id,
            len(self._acquired_ids),
        )

    def signal_shutdown(self) -> None:
        """Signal the guard to abort any waiting acquires (clean shutdown)."""
        self._shutdown_event.set()

    def active_sessions(self) -> int:
        """Number of currently acquired permits (explicit counter, no Semaphore internals)."""
        return len(self._acquired_ids)

    def capacity(self) -> int:
        """Total capacity as computed at construction time."""
        return self._capacity

    def mem_available_mb(self) -> int | None:
        """Current MemAvailable via the injected reader."""
        return self._reader.mem_available_mb()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    async def _acquire_semaphore(self, context_id: str) -> None:
        """Acquire the semaphore with a bounded wait so shutdown is honored.

        F-02: a check-then-act (probe `_value` then `acquire()`) races under N
        workers — two coroutines both observe a free slot, one wins, the other
        blocks indefinitely inside `acquire()` with no shutdown check. Instead we
        wait on `acquire()` itself with a per-iteration timeout and re-check the
        shutdown event between attempts. No internals probing.
        """
        while True:
            if self._shutdown_event.is_set():
                raise BrowserAdmissionDenied(
                    f"Shutdown signaled while waiting for semaphore "
                    f"for context_id={context_id}"
                )
            try:
                await asyncio.wait_for(
                    self._sem.acquire(), timeout=self._recheck_interval_s
                )
                return
            except TimeoutError:
                continue

    async def _dynamic_memory_guard(self, context_id: str) -> None:
        """Park while memory pressure is too high for a new session.

        If MemAvailable is unreadable (mem_available_mb() returns None),
        the dynamic guard is skipped — the semaphore-only limit applies.
        This is static-semaphore-only mode: logged once at boot, never crash.
        """
        budget_mb = self._session_mb + self._headroom_mb
        warned = False

        while True:
            if self._shutdown_event.is_set():
                # Undo the semaphore acquire before raising.
                self._acquired_ids.discard(context_id)
                self._sem.release()
                raise BrowserAdmissionDenied(
                    f"Shutdown signaled while waiting for RAM for context_id={context_id}"
                )

            mem = self._reader.mem_available_mb()
            if mem is None or mem >= budget_mb:
                return

            if not warned:
                logger.warning(
                    "hermes.execution.browser_admission.memory_pressure "
                    "context_id=%s mem_available=%dMB budget=%dMB — parking",
                    context_id,
                    mem,
                    budget_mb,
                )
                warned = True

            await asyncio.sleep(self._recheck_interval_s)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
