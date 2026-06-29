"""Unit tests for GET /api/v1/runtime/agent-stats (Fase 5 — agent stats endpoint).

Coverage:
  - Working agent (appears in activity[]) is marked state="working".
  - Active via active_agent_id is also marked "working".
  - Agents with no activity are "idle".
  - today.tokens / today.cost_usd / today.tasks are populated from usage_repo.
  - Agents with no today usage default to zeros (not None).
  - agents is always a list (never None) — prevents undefined.length in frontend.
  - D-Bus unavailable → available=false, agents=[].
  - usage_repo error → agents present but today zeros.
  - Runtime status missing activity key → no crash.

All tests are pure unit tests: no real D-Bus, no real DB connections for the
proxy.  The usage repo writes to a tmp_path SQLite file (real but isolated).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes.domain.cycle_output import TokenUsage
from hermes.shell_server.metering.agent_stats_api import (
    _active_agent_ids,
    _agent_stat,
    _dedup_key,
    _EMPTY_TODAY,
    _build_agent_stats,
)
from hermes.shell_server.metering.usage_repo import SQLiteUsageRepository

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# SSE stream dedup — runtime.captured_at must NOT count as a change
# (regression: a fresh per-call timestamp defeated the dedup, re-pushing a full
#  frame every tick and starving the keepalive branch).
# ---------------------------------------------------------------------------
def test_dedup_key_ignores_captured_at():
    """Two snapshots that differ ONLY in runtime.captured_at have the same key."""
    base = {
        "runtime": {"state": "idle", "active_task_count": 0, "available": True},
        "stats": {"available": True, "agents": []},
    }
    a = {"runtime": {**base["runtime"], "captured_at": "2026-06-29T00:00:00+00:00"}, "stats": base["stats"]}
    b = {"runtime": {**base["runtime"], "captured_at": "2026-06-29T00:00:02+00:00"}, "stats": base["stats"]}
    assert _dedup_key(a) == _dedup_key(b)


def test_dedup_key_detects_real_state_change():
    """A real change (idle → working) yields a different key."""
    idle = {
        "runtime": {"state": "idle", "active_task_count": 0, "captured_at": "t1"},
        "stats": {"available": True, "agents": []},
    }
    working = {
        "runtime": {"state": "working", "active_task_count": 1, "captured_at": "t2"},
        "stats": {"available": True, "agents": []},
    }
    assert _dedup_key(idle) != _dedup_key(working)


def test_dedup_key_detects_stats_change():
    """A change in per-agent stats (today usage) also yields a different key."""
    a = {"runtime": {"state": "idle", "captured_at": "t1"}, "stats": {"available": True, "agents": [{"agent_id": "x", "today": {"tasks": 0}}]}}
    b = {"runtime": {"state": "idle", "captured_at": "t2"}, "stats": {"available": True, "agents": [{"agent_id": "x", "today": {"tasks": 3}}]}}
    assert _dedup_key(a) != _dedup_key(b)


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_usage(
    *,
    prompt: int = 100,
    completion: int = 50,
    cost: float = 0.005,
    model: str = "qwen3",
) -> TokenUsage:
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost_usd=cost,
        model=model,
        cost_status="billed",
        cost_source="litellm",
        provider="vllm",
    )


def _repo(tmp_path: Path) -> SQLiteUsageRepository:
    return SQLiteUsageRepository(db_path=tmp_path / "stats.db")


def _fake_proxy(
    *,
    status: dict[str, Any] | None = None,
    agents: list[dict[str, Any]] | None = None,
    status_raises: Exception | None = None,
    agents_raises: Exception | None = None,
) -> MagicMock:
    """Construct a fake DbusRuntimeProxy for tests."""
    proxy = MagicMock()

    if status_raises is not None:
        proxy.call_dict = AsyncMock(side_effect=status_raises)
    else:
        proxy.call_dict = AsyncMock(return_value=status or {})

    if agents_raises is not None:
        proxy.call_list = AsyncMock(side_effect=agents_raises)
    else:
        proxy.call_list = AsyncMock(return_value=agents or [])

    return proxy


def _fake_request(proxy: MagicMock) -> MagicMock:
    """Construct a fake FastAPI Request with app.state.dbus_proxy set."""
    req = MagicMock()
    req.app.state.dbus_proxy = proxy
    return req


# ---------------------------------------------------------------------------
# Pure-function tests (no I/O)
# ---------------------------------------------------------------------------


class TestActiveAgentIds:
    def test_extracts_from_activity_list(self) -> None:
        status = {
            "activity": [
                {"agent_id": "agent-A", "tool": "read_file"},
                {"agent_id": "agent-B"},
            ]
        }
        ids = _active_agent_ids(status)
        assert "agent-A" in ids
        assert "agent-B" in ids

    def test_extracts_active_agent_id_field(self) -> None:
        status = {"active_agent_id": "ceo", "activity": []}
        assert "ceo" in _active_agent_ids(status)

    def test_empty_status_returns_empty_set(self) -> None:
        assert _active_agent_ids({}) == frozenset()

    def test_activity_entries_without_agent_id_are_skipped(self) -> None:
        status = {"activity": [{"tool": "browse"}, None]}
        # No crash and no spurious entries.
        ids = _active_agent_ids(status)
        assert ids == frozenset()

    def test_deduplication(self) -> None:
        status = {
            "active_agent_id": "agent-A",
            "activity": [{"agent_id": "agent-A"}],
        }
        ids = _active_agent_ids(status)
        assert ids == frozenset({"agent-A"})


class TestAgentStatShape:
    _AGENT = {
        "agent_id": "agent-X",
        "name": "Desmond",
        "department": "research",
        "color": "#ff0000",
    }

    def test_idle_agent_when_not_in_active_ids(self) -> None:
        stat = _agent_stat(self._AGENT, active_ids=frozenset(), today_map={})
        assert stat["state"] == "idle"
        assert stat["active_task_count"] == 0

    def test_working_agent_when_in_active_ids(self) -> None:
        stat = _agent_stat(
            self._AGENT,
            active_ids=frozenset({"agent-X"}),
            today_map={},
        )
        assert stat["state"] == "working"
        assert stat["active_task_count"] == 1

    def test_today_defaults_to_zeros_when_absent(self) -> None:
        stat = _agent_stat(self._AGENT, active_ids=frozenset(), today_map={})
        assert stat["today"] == {"tokens": 0, "cost_usd": 0.0, "tasks": 0}

    def test_today_populated_from_map(self) -> None:
        today_map = {"agent-X": {"tokens": 500, "cost_usd": 0.01, "tasks": 3}}
        stat = _agent_stat(
            self._AGENT, active_ids=frozenset(), today_map=today_map
        )
        assert stat["today"]["tokens"] == 500
        assert stat["today"]["cost_usd"] == pytest.approx(0.01)
        assert stat["today"]["tasks"] == 3

    def test_health_ok_when_tasks_gt_zero(self) -> None:
        today_map = {"agent-X": {"tokens": 100, "cost_usd": 0.0, "tasks": 1}}
        stat = _agent_stat(
            self._AGENT, active_ids=frozenset(), today_map=today_map
        )
        assert stat["health"] == "ok"

    def test_health_unknown_when_no_tasks(self) -> None:
        stat = _agent_stat(self._AGENT, active_ids=frozenset(), today_map={})
        assert stat["health"] == "unknown"

    def test_none_department_becomes_empty_string(self) -> None:
        agent = dict(self._AGENT) | {"department": None}
        stat = _agent_stat(agent, active_ids=frozenset(), today_map={})
        assert stat["department"] == ""

    def test_none_color_becomes_empty_string(self) -> None:
        agent = dict(self._AGENT) | {"color": None}
        stat = _agent_stat(agent, active_ids=frozenset(), today_map={})
        assert stat["color"] == ""

    def test_all_required_keys_present(self) -> None:
        stat = _agent_stat(self._AGENT, active_ids=frozenset(), today_map={})
        for key in ("agent_id", "name", "department", "color", "state",
                    "active_task_count", "today", "health"):
            assert key in stat, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# SQLiteUsageRepository.today_by_agent tests
# ---------------------------------------------------------------------------


class TestTodayByAgent:
    def test_empty_when_no_data(self, tmp_path: Path) -> None:
        result = _repo(tmp_path).today_by_agent()
        assert result == {}

    def test_returns_today_usage(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        repo.record_cycle(
            agent_id="agent-1",
            conversation_id="conv-1",
            task_id="task-1",
            usage=_make_usage(prompt=200, completion=100, cost=0.005),
            tool_calls=2,
            latency_ms=500,
            outcome="completed",
        )
        result = repo.today_by_agent()
        assert "agent-1" in result
        entry = result["agent-1"]
        assert entry["tokens"] == 300
        assert entry["cost_usd"] == pytest.approx(0.005)
        assert entry["tasks"] == 1

    def test_accumulates_multiple_cycles_same_agent(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        usage = _make_usage(prompt=100, completion=50, cost=0.002)
        for _ in range(3):
            repo.record_cycle(
                agent_id="agent-A",
                conversation_id=None,
                task_id=None,
                usage=usage,
                tool_calls=0,
                latency_ms=None,
                outcome="completed",
            )
        result = repo.today_by_agent()
        assert result["agent-A"]["tasks"] == 3
        assert result["agent-A"]["tokens"] == 450

    def test_separates_different_agents(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        repo.record_cycle(
            agent_id="agent-A",
            conversation_id=None,
            task_id=None,
            usage=_make_usage(cost=0.01),
            tool_calls=0,
            latency_ms=None,
            outcome="completed",
        )
        repo.record_cycle(
            agent_id="agent-B",
            conversation_id=None,
            task_id=None,
            usage=_make_usage(cost=0.03),
            tool_calls=0,
            latency_ms=None,
            outcome="completed",
        )
        result = repo.today_by_agent()
        assert set(result.keys()) == {"agent-A", "agent-B"}

    def test_null_agent_id_excluded(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        repo.record_cycle(
            agent_id=None,
            conversation_id=None,
            task_id=None,
            usage=_make_usage(),
            tool_calls=0,
            latency_ms=None,
            outcome="completed",
        )
        result = repo.today_by_agent()
        assert None not in result


# ---------------------------------------------------------------------------
# Integration tests for _build_agent_stats (async, fake proxy + real DB)
# ---------------------------------------------------------------------------


class TestBuildAgentStats:
    def test_working_agent_in_activity(self, tmp_path: Path) -> None:
        """Agent appearing in activity[] is marked working with today usage."""
        repo = SQLiteUsageRepository(db_path=tmp_path / "s.db")
        repo.record_cycle(
            agent_id="agent-W",
            conversation_id=None,
            task_id=None,
            usage=_make_usage(prompt=300, completion=150, cost=0.008),
            tool_calls=1,
            latency_ms=200,
            outcome="completed",
        )

        proxy = _fake_proxy(
            status={
                "state": "working",
                "active_task_count": 1,
                "activity": [{"agent_id": "agent-W", "tool": "think"}],
                "active_agent_id": "agent-W",
            },
            agents=[
                {
                    "agent_id": "agent-W",
                    "name": "Worker",
                    "department": "ops",
                    "color": "#aabbcc",
                }
            ],
        )
        req = _fake_request(proxy)

        db_path = tmp_path / "s.db"
        with patch(
            "hermes.shell_server.metering.agent_stats_api._DB_PATH", db_path
        ):
            result = asyncio.run(_build_agent_stats(req))

        assert result["available"] is True
        assert len(result["agents"]) == 1
        agent = result["agents"][0]
        assert agent["state"] == "working"
        assert agent["today"]["tokens"] == 450
        assert agent["today"]["cost_usd"] == pytest.approx(0.008)
        assert agent["today"]["tasks"] == 1

    def test_idle_agent_has_zero_today_when_no_usage(self, tmp_path: Path) -> None:
        proxy = _fake_proxy(
            status={"state": "idle", "active_task_count": 0, "activity": []},
            agents=[
                {
                    "agent_id": "agent-I",
                    "name": "Idle",
                    "department": None,
                    "color": None,
                }
            ],
        )
        req = _fake_request(proxy)

        db_path = tmp_path / "empty.db"
        with patch(
            "hermes.shell_server.metering.agent_stats_api._DB_PATH", db_path
        ):
            result = asyncio.run(_build_agent_stats(req))

        assert result["agents"][0]["state"] == "idle"
        assert result["agents"][0]["today"] == _EMPTY_TODAY

    def test_dbus_unavailable_returns_available_false_empty_agents(
        self, tmp_path: Path
    ) -> None:
        proxy = _fake_proxy(
            status_raises=Exception("D-Bus not available"),
            agents_raises=Exception("D-Bus not available"),
        )
        req = _fake_request(proxy)

        db_path = tmp_path / "empty.db"
        with patch(
            "hermes.shell_server.metering.agent_stats_api._DB_PATH", db_path
        ):
            result = asyncio.run(_build_agent_stats(req))

        assert result["available"] is False
        assert result["agents"] == []

    def test_agents_always_list_never_none(self, tmp_path: Path) -> None:
        """agents key must always be a list to prevent frontend .length crash."""
        proxy = _fake_proxy(status={}, agents=[])
        req = _fake_request(proxy)

        db_path = tmp_path / "empty.db"
        with patch(
            "hermes.shell_server.metering.agent_stats_api._DB_PATH", db_path
        ):
            result = asyncio.run(_build_agent_stats(req))

        assert isinstance(result["agents"], list)
        assert result["agents"] is not None

    def test_usage_repo_error_still_returns_agents_with_zero_today(
        self, tmp_path: Path
    ) -> None:
        proxy = _fake_proxy(
            status={"state": "idle", "active_task_count": 0, "activity": []},
            agents=[
                {
                    "agent_id": "agent-Z",
                    "name": "Zara",
                    "department": "ops",
                    "color": "#123456",
                }
            ],
        )
        req = _fake_request(proxy)

        # Point _DB_PATH to a directory (invalid DB path) to force repo error.
        with patch(
            "hermes.shell_server.metering.agent_stats_api._fetch_today_map",
            side_effect=Exception("DB exploded"),
        ):
            result = asyncio.run(_build_agent_stats(req))

        # agents is still a list with the agent present; the endpoint does not 500.
        assert isinstance(result["agents"], list)
        assert len(result["agents"]) == 1
        assert result["agents"][0]["today"] == _EMPTY_TODAY

    def test_active_agent_id_field_marks_working(self, tmp_path: Path) -> None:
        """active_agent_id alone (no activity list entry) is enough for working."""
        proxy = _fake_proxy(
            status={
                "state": "working",
                "active_task_count": 1,
                "active_agent_id": "ceo",
                "activity": [],
            },
            agents=[{"agent_id": "ceo", "name": "CEO", "department": None, "color": None}],
        )
        req = _fake_request(proxy)

        db_path = tmp_path / "empty.db"
        with patch(
            "hermes.shell_server.metering.agent_stats_api._DB_PATH", db_path
        ):
            result = asyncio.run(_build_agent_stats(req))

        assert result["agents"][0]["state"] == "working"

    def test_runtime_status_without_activity_key_no_crash(
        self, tmp_path: Path
    ) -> None:
        """Status dict missing activity key must not raise KeyError."""
        proxy = _fake_proxy(
            # No "activity" key in status
            status={"state": "idle", "active_task_count": 0},
            agents=[{"agent_id": "x", "name": "X", "department": None, "color": None}],
        )
        req = _fake_request(proxy)

        db_path = tmp_path / "empty.db"
        with patch(
            "hermes.shell_server.metering.agent_stats_api._DB_PATH", db_path
        ):
            result = asyncio.run(_build_agent_stats(req))

        assert isinstance(result["agents"], list)
