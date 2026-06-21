"""Unit tests — BrowserSessionRegistry.

Verifies: get, put, pop, lock_for semantics.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from hermes.browser.application.browser_session_registry import (
    BrowserSessionRegistry,
    BrowserTaskSession,
)

pytestmark = pytest.mark.unit


def _fake_cli():
    """Return a minimal stand-in for AgentBrowserCli that satisfies type narrowing."""

    class _Stub:
        pass

    return _Stub()


def _session(context_id=None, site_id=None):
    return BrowserTaskSession(
        context_id=context_id or uuid4(),
        cli=_fake_cli(),  # type: ignore[arg-type]
        site_id=site_id,
    )


class TestGetPutPop:
    def test_get_unknown_returns_none(self) -> None:
        reg = BrowserSessionRegistry()
        assert reg.get(uuid4()) is None

    def test_put_then_get(self) -> None:
        reg = BrowserSessionRegistry()
        wid = uuid4()
        sess = _session()
        reg.put(wid, sess)
        assert reg.get(wid) is sess

    def test_pop_removes_and_returns(self) -> None:
        reg = BrowserSessionRegistry()
        wid = uuid4()
        sess = _session()
        reg.put(wid, sess)
        popped = reg.pop(wid)
        assert popped is sess
        assert reg.get(wid) is None

    def test_pop_unknown_is_idempotent(self) -> None:
        reg = BrowserSessionRegistry()
        result = reg.pop(uuid4())
        assert result is None

    def test_put_replaces_existing(self) -> None:
        reg = BrowserSessionRegistry()
        wid = uuid4()
        s1 = _session()
        s2 = _session()
        reg.put(wid, s1)
        reg.put(wid, s2)
        assert reg.get(wid) is s2


class TestLockFor:
    def test_lock_for_same_id_returns_same_lock(self) -> None:
        reg = BrowserSessionRegistry()
        wid = uuid4()
        lock1 = reg.lock_for(wid)
        lock2 = reg.lock_for(wid)
        assert lock1 is lock2

    def test_lock_for_distinct_ids_returns_distinct_locks(self) -> None:
        reg = BrowserSessionRegistry()
        wid1, wid2 = uuid4(), uuid4()
        assert reg.lock_for(wid1) is not reg.lock_for(wid2)

    def test_lock_is_asyncio_lock(self) -> None:
        reg = BrowserSessionRegistry()
        lock = reg.lock_for(uuid4())
        assert isinstance(lock, asyncio.Lock)

    def test_lock_survives_pop(self) -> None:
        """Lock is NOT removed when the session is popped (racing replay safety)."""
        reg = BrowserSessionRegistry()
        wid = uuid4()
        sess = _session()
        reg.put(wid, sess)
        lock_before = reg.lock_for(wid)
        reg.pop(wid)
        lock_after = reg.lock_for(wid)
        assert lock_before is lock_after
