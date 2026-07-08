"""Unit tests — Skill Hub realtime search cancel logic.

Verifies:
  - _hub_search_register returns a threading.Event.
  - _hub_search_cancel sets that event.
  - _hub_search_cleanup removes the entry.
  - search_skills_hub returns {cancelled: True} when the event is set before
    unified_search returns (simulated with a side-effect).
  - cancel_skills_hub_search returns {ok: True} for a known query_id.
  - cancel_skills_hub_search returns {ok: False} for an empty query_id.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    _hub_search_cancel,
    _hub_search_cleanup,
    _hub_search_get_cancel_event,
    _hub_search_register,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def test_register_returns_event():
    qid = "abc123"
    ev = _hub_search_register(qid)
    assert isinstance(ev, threading.Event)
    assert not ev.is_set()
    _hub_search_cleanup(qid)


def test_cancel_sets_event():
    qid = "def456"
    ev = _hub_search_register(qid)
    _hub_search_cancel(qid)
    assert ev.is_set()
    _hub_search_cleanup(qid)


def test_cleanup_removes_entry():
    qid = "ghi789"
    _hub_search_register(qid)
    _hub_search_cleanup(qid)
    assert _hub_search_get_cancel_event(qid) is None


def test_cancel_unknown_id_is_noop():
    # Must not raise.
    _hub_search_cancel("nonexistent-id")


def test_get_cancel_event_unknown_returns_none():
    assert _hub_search_get_cancel_event("no-such-id") is None


# ---------------------------------------------------------------------------
# search_skills_hub — cancel path
# ---------------------------------------------------------------------------


def _make_mock_meta(name: str) -> Any:
    m = MagicMock()
    m.name = name
    m.description = "desc"
    m.source = "github"
    m.identifier = f"gh/{name}"
    m.trust_level = "community"
    m.repo = "https://example.com"
    m.tags = []
    return m


def _build_wiring() -> Any:
    """Return a minimal DbusRuntimeServiceWiring instance (no ports needed).

    search_skills_hub / cancel_skills_hub_search only touch the module-level
    _hub_search_* helpers, so the wiring needs no real ports — only the three
    required constructor kwargs (agent_state, approval_gate, authorized_uids).
    """
    from hermes.agents_os.infrastructure.dbus_runtime_service import (
        DbusRuntimeServiceWiring,
    )
    return DbusRuntimeServiceWiring(
        agent_state=MagicMock(),
        approval_gate=MagicMock(),
        authorized_uids=frozenset({1000}),
        skill_governance=MagicMock(),
    )


def test_search_returns_cancelled_when_event_set():
    """If cancel event is set before unified_search returns, result is cancelled."""
    qid = "cancel-test-001"
    ev = _hub_search_register(qid)
    ev.set()  # simulate cancel arriving before unified_search finishes

    fake_meta = _make_mock_meta("email-skill")

    # Exercise the REAL search_skills_hub to hit the cancel-event branch.
    # Only tools.skills_hub is stubbed; the method itself must run unpatched.
    with patch.dict("sys.modules", {"tools.skills_hub": MagicMock()}):
        import sys
        hub_mod = sys.modules["tools.skills_hub"]
        hub_mod.create_source_router.return_value = object()
        hub_mod.unified_search.return_value = [fake_meta]

        wiring = _build_wiring()
        result = wiring.search_skills_hub(
            query="email", source="all", limit=10, query_id=qid
        )

    assert result["cancelled"] is True
    assert result["results"] == []
    _hub_search_cleanup(qid)


def test_search_returns_results_when_not_cancelled():
    """Normal path: results are returned when no cancel event fires."""
    qid = "no-cancel-001"
    _hub_search_register(qid)
    # Do NOT cancel.

    fake_meta = _make_mock_meta("calendar-skill")

    with patch.dict("sys.modules", {"tools.skills_hub": MagicMock()}):
        import sys
        hub_mod = sys.modules["tools.skills_hub"]
        hub_mod.create_source_router.return_value = object()
        hub_mod.unified_search.return_value = [fake_meta]

        wiring = _build_wiring()
        result = wiring.search_skills_hub(
            query="calendar", source="all", limit=10, query_id=qid
        )

    assert result["cancelled"] is False
    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "calendar-skill"
    _hub_search_cleanup(qid)


# ---------------------------------------------------------------------------
# cancel_skills_hub_search wiring method
# ---------------------------------------------------------------------------


def test_cancel_method_ok():
    qid = "cancel-via-method-001"
    _hub_search_register(qid)
    wiring = _build_wiring()
    out = wiring.cancel_skills_hub_search(query_id=qid)
    assert out == {"ok": True}
    ev = _hub_search_get_cancel_event(qid)
    assert ev is not None and ev.is_set()
    _hub_search_cleanup(qid)


def test_cancel_method_empty_id_returns_error():
    wiring = _build_wiring()
    out = wiring.cancel_skills_hub_search(query_id="")
    assert out["ok"] is False
    assert "query_id" in out["error"]
