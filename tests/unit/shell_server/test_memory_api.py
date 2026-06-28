"""Unit tests for the Memory REST API.

Coverage:
  - GET /api/v1/memory — list returns content_truncated (not full text)
  - GET /api/v1/memory/{entry_id} — detail returns full content; 404 on not found
  - GET /api/v1/memory/{entry_id} — 503 on daemon unavailable
  - DELETE /api/v1/memory/{entry_id} — idempotent delete (existing behaviour)
  - Routing: /search does not collide with /{entry_id}
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.cowork.memory_api import create_memory_router
from hermes.tasks.control_plane.domain.ports import AgentUnavailable

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(proxy: MagicMock) -> FastAPI:
    app = FastAPI()
    app.state.dbus_proxy = proxy
    app.include_router(create_memory_router())
    return app


def _proxy(*, list_return=None, get_return=None, delete_return=None) -> MagicMock:
    """Return a minimal mock dbus proxy."""
    p = MagicMock()
    p.call_list = AsyncMock(return_value=list_return or [])
    p.call_dict = AsyncMock(return_value=get_return or {})
    return p


# ---------------------------------------------------------------------------
# GET /api/v1/memory — list
# ---------------------------------------------------------------------------


class TestListMemory:
    def test_returns_list(self) -> None:
        entries = [
            {
                "id": "memory:0",
                "target": "memory",
                "content_truncated": "short preview",
                "entry_index": 0,
            }
        ]
        client = TestClient(_make_app(_proxy(list_return=entries)))
        r = client.get("/api/v1/memory")
        assert r.status_code == 200
        assert r.json() == entries

    def test_fail_soft_returns_empty_list_on_unavailable(self) -> None:
        p = MagicMock()
        p.call_list = AsyncMock(side_effect=AgentUnavailable("daemon down"))
        client = TestClient(_make_app(p))
        r = client.get("/api/v1/memory")
        assert r.status_code == 200
        assert r.json() == []

    def test_content_truncated_field_not_renamed(self) -> None:
        entries = [
            {
                "id": "facts:1",
                "target": "facts",
                "content_truncated": "x" * 200,
                "entry_index": 1,
            }
        ]
        client = TestClient(_make_app(_proxy(list_return=entries)))
        body = client.get("/api/v1/memory").json()
        assert "content_truncated" in body[0]
        # Full content field must NOT be present in list response.
        assert "content" not in body[0]


# ---------------------------------------------------------------------------
# GET /api/v1/memory/{entry_id} — detail
# ---------------------------------------------------------------------------


class TestGetMemoryEntry:
    def test_returns_full_entry(self) -> None:
        full_entry = {
            "id": "memory:0",
            "target": "memory",
            "content": "The full, untruncated content of this memory entry.",
            "entry_index": 0,
        }
        client = TestClient(_make_app(_proxy(get_return=full_entry)))
        r = client.get("/api/v1/memory/memory:0")
        assert r.status_code == 200
        body = r.json()
        assert body["content"] == full_entry["content"]
        assert body["id"] == "memory:0"
        assert body["target"] == "memory"

    def test_not_found_when_daemon_returns_empty(self) -> None:
        client = TestClient(_make_app(_proxy(get_return={})))
        r = client.get("/api/v1/memory/memory:999")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "not_found"

    def test_503_on_agent_unavailable(self) -> None:
        p = MagicMock()
        p.call_dict = AsyncMock(side_effect=AgentUnavailable("daemon down"))
        client = TestClient(_make_app(p))
        r = client.get("/api/v1/memory/memory:0")
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "agent_unavailable"

    def test_entry_id_passed_through_to_proxy(self) -> None:
        full_entry = {
            "id": "facts:3",
            "target": "facts",
            "content": "content here",
            "entry_index": 3,
        }
        p = _proxy(get_return=full_entry)
        client = TestClient(_make_app(p))
        client.get("/api/v1/memory/facts:3")
        p.call_dict.assert_called_once_with("get_memory_entry", "facts:3")


# ---------------------------------------------------------------------------
# Routing: /search must not be shadowed by /{entry_id}
# ---------------------------------------------------------------------------


class TestSearchRouteNotShadowed:
    def test_search_route_is_reachable(self) -> None:
        search_results = [{"id": "memory:0", "target": "memory", "content_truncated": "test"}]
        p = MagicMock()
        p.call_list = AsyncMock(return_value=search_results)
        client = TestClient(_make_app(p))
        r = client.get("/api/v1/memory/search?q=test")
        assert r.status_code == 200
        assert r.json() == search_results


# ---------------------------------------------------------------------------
# DELETE /api/v1/memory/{entry_id} — existing behaviour regression guard
# ---------------------------------------------------------------------------


class TestDeleteMemoryEntry:
    def test_idempotent_delete_returns_ok(self) -> None:
        p = MagicMock()
        p.call_dict = AsyncMock(return_value={"ok": True, "deleted": True})
        client = TestClient(_make_app(p))
        r = client.delete("/api/v1/memory/memory:0")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_503_on_agent_unavailable(self) -> None:
        p = MagicMock()
        p.call_dict = AsyncMock(side_effect=AgentUnavailable("daemon down"))
        client = TestClient(_make_app(p))
        r = client.delete("/api/v1/memory/memory:0")
        assert r.status_code == 503

    def test_delete_uses_forget_verb(self) -> None:
        p = MagicMock()
        p.call_dict = AsyncMock(return_value={"ok": True})
        client = TestClient(_make_app(p))
        client.delete("/api/v1/memory/memory:5")
        # The DELETE endpoint calls forget_memory_entry; GET calls
        # get_memory_entry (dbus-fast exposes call_<snake_case> — a PascalCase
        # verb fails closed → 503/empty UI; see test_dbus_proxy_verb_names).
        calls = [call.args[0] for call in p.call_dict.call_args_list]
        assert "forget_memory_entry" in calls
