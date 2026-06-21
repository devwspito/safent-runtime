"""BrowserSessionRegistry — per-task session tracking for BrowserSurfaceAdapter.

Holds the mapping: work_item_id (UUID) → BrowserTaskSession, plus one
asyncio.Lock per task so that concurrent replays for the same work_item_id
serialize the get-or-open path without global serialization.

Design:
  - BrowserTaskSession is a named tuple — immutable once stored.
  - Separate dict for per-task locks (lazily created) to avoid coupling
    session lifetime to lock lifetime.
  - No god-class: only get, lock_for, put, pop. All logic above (open/close
    the factory, execute CLI ops) lives in BrowserSurfaceAdapter.

Thread-safety: this registry is shared by concurrent asyncio workers.
asyncio.Lock is not thread-safe, but we run in a single-threaded event loop
(standard asyncio). If threading is introduced later, replace asyncio.Lock
with threading.Lock here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from hermes.browser.infrastructure.agent_browser_cli import AgentBrowserCli


@dataclass(frozen=True, slots=True)
class BrowserTaskSession:
    """Snapshot of an open browser session for one work_item_id.

    Attributes:
        context_id: UUID used with IsolatedExecutionContextFactory.
        cli: live AgentBrowserCli for the session.
        site_id: hostname of the site currently active in the session,
            or None if no navigation has occurred yet.
    """

    context_id: UUID
    cli: AgentBrowserCli
    site_id: str | None


class BrowserSessionRegistry:
    """Registry of open browser sessions keyed by work_item_id.

    Public API:
        get(work_item_id)     → BrowserTaskSession | None
        lock_for(work_item_id)→ asyncio.Lock  (lazily created, per-task)
        put(work_item_id, session)
        pop(work_item_id)     → BrowserTaskSession | None (idempotent)
    """

    def __init__(self) -> None:
        self._sessions: dict[UUID, BrowserTaskSession] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}

    def get(self, work_item_id: UUID) -> BrowserTaskSession | None:
        """Return the session for work_item_id, or None if not open."""
        return self._sessions.get(work_item_id)

    def lock_for(self, work_item_id: UUID) -> asyncio.Lock:
        """Return (lazily creating) the per-task asyncio.Lock.

        The lock is NOT removed on pop() intentionally — a racing replay
        that holds the lock after pop() will find no session and open a
        fresh one, which is correct behaviour.
        """
        if work_item_id not in self._locks:
            self._locks[work_item_id] = asyncio.Lock()
        return self._locks[work_item_id]

    def put(self, work_item_id: UUID, session: BrowserTaskSession) -> None:
        """Store session for work_item_id (replaces any existing entry)."""
        self._sessions[work_item_id] = session

    def pop(self, work_item_id: UUID) -> BrowserTaskSession | None:
        """Remove and return the session for work_item_id.

        Idempotent: returns None for an unknown work_item_id without raising.
        """
        return self._sessions.pop(work_item_id, None)
