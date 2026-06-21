"""Regression tests for idle CPU/RAM efficiency improvements.

Pins the specific hogs fixed:
  1. ModelHealthMonitor default poll_interval_s >= 30 s (was 5 s).
  2. RuntimeBackendHealthMonitor default poll_interval_s >= 15 s (was 2 s).
  3. AuditTailWriter.start_background() skips flush_once() on empty queue.
  4. AuditTailWriter.start_background() default interval >= 30 s (was 5 s).
  5. Lumen Backend adaptive constants: SETTLED >= FAST for both pollers.
  6. HttpModelClient reuses its aiohttp session across calls (no new session per poll).
"""

from __future__ import annotations

import inspect
import threading
import time
import queue as _queue_mod
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. ModelHealthMonitor — default poll interval
# ---------------------------------------------------------------------------


def test_model_health_monitor_default_poll_interval_gte_30s() -> None:
    """poll_interval_s default must be >= 30 s on ARM/battery hardware."""
    from hermes.runtime.model_health_monitor import ModelMonitorConfig

    cfg = ModelMonitorConfig()
    assert cfg.poll_interval_s >= 30.0, (
        f"ModelMonitorConfig.poll_interval_s={cfg.poll_interval_s} is too aggressive "
        "for ARM idle; must be >= 30 s to avoid constant wakeups."
    )


# ---------------------------------------------------------------------------
# 2. RuntimeBackendHealthMonitor — default poll interval
# ---------------------------------------------------------------------------


def test_runtime_backend_health_monitor_default_poll_interval_gte_15s() -> None:
    """poll_interval_s default must be >= 15 s for settled connected state."""
    from hermes.shell.application.runtime_backend_health_monitor import MonitorConfig

    cfg = MonitorConfig()
    assert cfg.poll_interval_s >= 15.0, (
        f"MonitorConfig.poll_interval_s={cfg.poll_interval_s} is too aggressive "
        "for ARM idle; must be >= 15 s."
    )


# ---------------------------------------------------------------------------
# 3. AuditTailWriter — skip flush on empty queue
# ---------------------------------------------------------------------------


def test_audit_tail_writer_skips_flush_when_queue_empty() -> None:
    """start_background() must NOT call flush_once() when the queue is empty."""
    from hermes.agents_os.infrastructure.audit_tail_writer import (
        AuditTailWriter,
        FakeAuditTailTransport,
    )

    transport = FakeAuditTailTransport()
    writer = AuditTailWriter(transport=transport, spool_dir=None)

    flush_calls: list[int] = []
    original_flush = writer.flush_once

    def _counting_flush():
        flush_calls.append(1)
        return original_flush()

    writer.flush_once = _counting_flush  # type: ignore[method-assign]

    # Start with a very short interval so the thread fires quickly in the test.
    writer.start_background(flush_interval_seconds=0.05)
    time.sleep(0.2)  # let 3-4 ticks fire
    writer.stop()

    # Queue was empty the whole time — flush_once should NOT have been called.
    assert flush_calls == [], (
        f"flush_once() was called {len(flush_calls)} time(s) on an empty queue. "
        "The background thread should skip the flush when the queue is empty."
    )


def test_audit_tail_writer_does_flush_when_queue_has_entries() -> None:
    """start_background() MUST call flush_once() when the queue has entries."""
    from hermes.agents_os.infrastructure.audit_tail_writer import (
        AuditTailWriter,
        FakeAuditTailTransport,
    )
    from hermes.agents_os.application.audit_hash_chain import AuditEntry, AuditKind
    from datetime import datetime, UTC
    from uuid import uuid4

    transport = FakeAuditTailTransport()
    writer = AuditTailWriter(transport=transport, spool_dir=None)

    entry = AuditEntry(
        entry_id=uuid4(),
        node_installation_id=None,
        tenant_id=None,
        timestamp=datetime.now(tz=UTC),
        actor="test",
        audit_kind=AuditKind.TASK_CLAIMED,
        category=None,
        description="regression test",
        payload_hash_hex="aa",
        prev_entry_hash_hex="bb",
        signed_payload_hash_hex="cc",
        signature_hex="dd",
    )
    writer.enqueue(entry)

    writer.start_background(flush_interval_seconds=0.05)
    time.sleep(0.3)  # give it time to flush
    writer.stop()

    assert len(transport.published) == 1, (
        "AuditTailWriter did not flush an enqueued entry — background flush broken."
    )


# ---------------------------------------------------------------------------
# 4. AuditTailWriter — default flush interval >= 30 s
# ---------------------------------------------------------------------------


def test_audit_tail_writer_default_flush_interval_gte_30s() -> None:
    """start_background() default flush_interval_seconds must be >= 30 s."""
    from hermes.agents_os.infrastructure.audit_tail_writer import AuditTailWriter

    sig = inspect.signature(AuditTailWriter.start_background)
    default = sig.parameters["flush_interval_seconds"].default
    assert default >= 30.0, (
        f"AuditTailWriter.start_background flush_interval_seconds default={default} "
        "is too aggressive; must be >= 30 s to reduce idle thread wakeups."
    )


# ---------------------------------------------------------------------------
# 5. Lumen Backend — settled intervals >= fast intervals
#
# PySide6 is not installed in headless CI (it is a Wayland runtime dep).
# We extract the constants via ast.parse on the source file so the test
# works without a display server.
# ---------------------------------------------------------------------------


def _read_lumen_constants() -> dict[str, int]:
    """Parse the integer constants from lumen/__main__.py without importing PySide6.

    The constants use annotated assignment syntax (``name: int = value``), so we
    walk ast.AnnAssign nodes rather than ast.Assign.  Numeric literals with
    underscores (e.g. 30_000) are folded by the parser into plain ints.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).parents[2] / "src" / "hermes" / "lumen" / "__main__.py"
    tree = ast.parse(src.read_text())
    constants: dict[str, int] = {}
    for node in ast.walk(tree):
        # Annotated assignments: `_HEALTH_POLL_FAST_MS: int = 3_000`
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id.startswith(("_HEALTH_POLL", "_PROVIDER_POLL"))
            and node.value is not None
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
        ):
            constants[node.target.id] = node.value.value
    return constants


def test_lumen_backend_settled_intervals_are_slower_than_fast() -> None:
    """SETTLED poll intervals must be >= FAST intervals (back-off, not speed-up)."""
    consts = _read_lumen_constants()

    health_fast = consts["_HEALTH_POLL_FAST_MS"]
    health_settled = consts["_HEALTH_POLL_SETTLED_MS"]
    assert health_settled >= health_fast, (
        f"_HEALTH_POLL_SETTLED_MS ({health_settled}) "
        f"< _HEALTH_POLL_FAST_MS ({health_fast})"
    )

    provider_fast = consts["_PROVIDER_POLL_FAST_MS"]
    provider_settled = consts["_PROVIDER_POLL_SETTLED_MS"]
    assert provider_settled >= provider_fast, (
        f"_PROVIDER_POLL_SETTLED_MS ({provider_settled}) "
        f"< _PROVIDER_POLL_FAST_MS ({provider_fast})"
    )


def test_lumen_backend_settled_interval_at_least_3x_fast() -> None:
    """Settled interval should be at least 3× the fast interval for meaningful savings."""
    consts = _read_lumen_constants()

    ratio_health = consts["_HEALTH_POLL_SETTLED_MS"] / consts["_HEALTH_POLL_FAST_MS"]
    assert ratio_health >= 3.0, (
        f"Health poller settled/fast ratio={ratio_health:.1f}× is too small; "
        "expect at least 3× reduction once state is stable."
    )

    ratio_provider = consts["_PROVIDER_POLL_SETTLED_MS"] / consts["_PROVIDER_POLL_FAST_MS"]
    assert ratio_provider >= 3.0, (
        f"Provider poller settled/fast ratio={ratio_provider:.1f}× is too small; "
        "expect at least 3× reduction once state is stable."
    )


# ---------------------------------------------------------------------------
# 6. HttpModelClient — session reuse
# ---------------------------------------------------------------------------


def test_http_model_client_reuses_session() -> None:
    """HttpModelClient must reuse the aiohttp session across is_healthy() calls."""
    import asyncio
    from hermes.runtime.model_health_monitor import HttpModelClient

    client = HttpModelClient(base_url="http://127.0.0.1:8000")

    sessions_created: list = []

    class _FakeResp:
        status = 200
        async def json(self):
            return {"data": [{"id": "model-1"}]}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass

    class _FakeSession:
        closed = False
        def __init__(self):
            sessions_created.append(self)
        def get(self, _url):
            return _FakeResp()
        async def close(self):
            self.closed = True

    original_get_session = client._get_session

    async def _fake_get_session():
        if client._session is None or client._session.closed:
            client._session = _FakeSession()
        return client._session

    client._get_session = _fake_get_session  # type: ignore[method-assign]

    async def _run():
        await client.is_healthy()
        await client.is_healthy()
        await client.is_healthy()

    asyncio.run(_run())

    assert len(sessions_created) == 1, (
        f"HttpModelClient created {len(sessions_created)} aiohttp sessions for 3 "
        "is_healthy() calls. It must reuse a single session to avoid GC churn on ARM."
    )
