"""Unit tests for the managed-remote-endpoints REST surface on the MCP router.

Coverage:
  - GET /api/v1/mcp/managed-remote-endpoints — reads the local JSON store
    directly (no D-Bus), fail-soft to {} via the module's own load function.
  - PUT /api/v1/mcp/managed-remote-endpoints/{slug} — proxies
    set_managed_remote_endpoint(slug, url); a daemon {ok:false} (bad scheme,
    IP literal, disallowed port, unknown slug, ...) becomes a 400 via
    _raise_if_failed — NEVER a 200 (see feedback_mcp_add_ok_false_http201_silent:
    a 2xx never trips a caller's try/catch, so the failure passes silently).
    503 on AgentUnavailable.
  - POST /api/v1/mcp/managed-remote/{slug}/connect — two-step convenience:
    set_managed_remote_endpoint then add_mcp_server with the mcp-remote argv;
    short-circuits (never calls add_mcp_server) when step 1 fails, reporting
    that rejection as 400/403 too; force pass-through to the add_mcp_server
    draft; a blocked security-scan verdict (step 2) is 403, any other add
    failure is 400.
  - POST /api/v1/mcp (add_mcp_server) — same _raise_if_failed sweep: a
    rejected draft never reports the route's default 201.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.cowork.mcp_api import create_mcp_router
from hermes.tasks.control_plane.domain.ports import AgentUnavailable

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(proxy: MagicMock) -> FastAPI:
    app = FastAPI()
    app.state.dbus_proxy = proxy
    app.include_router(create_mcp_router())
    return app


def _proxy(*, mutator_return=None, mutator_side_effect=None) -> MagicMock:
    p = MagicMock()
    if mutator_side_effect is not None:
        p.call_mutator = AsyncMock(side_effect=mutator_side_effect)
    else:
        p.call_mutator = AsyncMock(return_value=mutator_return or {"ok": True})
    return p


# ---------------------------------------------------------------------------
# GET /api/v1/mcp/managed-remote-endpoints
# ---------------------------------------------------------------------------


class TestListManagedRemoteEndpoints:
    def test_returns_stored_endpoints(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hermes.shell_server.cowork.mcp_api.load_managed_remote_endpoints",
            lambda: {"safent-ads": "https://ads.tenant.ts.net/mcp"},
        )
        client = TestClient(_make_app(_proxy()))
        r = client.get("/api/v1/mcp/managed-remote-endpoints")
        assert r.status_code == 200
        assert r.json() == {"endpoints": {"safent-ads": "https://ads.tenant.ts.net/mcp"}}

    def test_empty_store_returns_empty_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hermes.shell_server.cowork.mcp_api.load_managed_remote_endpoints",
            dict,
        )
        client = TestClient(_make_app(_proxy()))
        r = client.get("/api/v1/mcp/managed-remote-endpoints")
        assert r.status_code == 200
        assert r.json() == {"endpoints": {}}

    def test_does_not_call_dbus(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hermes.shell_server.cowork.mcp_api.load_managed_remote_endpoints",
            dict,
        )
        p = _proxy()
        client = TestClient(_make_app(p))
        client.get("/api/v1/mcp/managed-remote-endpoints")
        p.call_mutator.assert_not_called()


# ---------------------------------------------------------------------------
# PUT /api/v1/mcp/managed-remote-endpoints/{slug}
# ---------------------------------------------------------------------------


class TestSetManagedRemoteEndpoint:
    def test_success_calls_correct_verb_with_slug_and_url(self) -> None:
        p = _proxy(mutator_return={"ok": True})
        client = TestClient(_make_app(p))
        r = client.put(
            "/api/v1/mcp/managed-remote-endpoints/safent-ads",
            json={"url": "https://ads.tenant.ts.net/mcp"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        p.call_mutator.assert_called_once_with(
            "set_managed_remote_endpoint", "safent-ads", "https://ads.tenant.ts.net/mcp"
        )

    @pytest.mark.parametrize(
        "error",
        [
            "managed_remote endpoint must use https:// (got 'http://')",
            "managed_remote endpoint must be a DNS name, not an IP literal: '10.0.0.1'",
            "managed_remote endpoint must use port 443 (got 8443)",
            "managed_remote endpoint must have a hostname",
        ],
    )
    def test_daemon_validation_error_returns_400(self, error: str) -> None:
        p = _proxy(mutator_return={"ok": False, "error": error})
        client = TestClient(_make_app(p))
        r = client.put(
            "/api/v1/mcp/managed-remote-endpoints/safent-ads",
            json={"url": "http://ads.example.com"},
        )
        assert r.status_code == 400
        body = r.json()
        assert body["detail"]["ok"] is False
        assert body["detail"]["error"] == error
        assert body["detail"]["message"] == error

    def test_unknown_slug_error_returns_400(self) -> None:
        p = _proxy(mutator_return={
            "ok": False,
            "error": "slug 'unknown' no es un servidor MANAGED_REMOTE conocido",
        })
        client = TestClient(_make_app(p))
        r = client.put(
            "/api/v1/mcp/managed-remote-endpoints/unknown",
            json={"url": "https://example.com"},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["ok"] is False

    def test_503_on_agent_unavailable(self) -> None:
        p = _proxy(mutator_side_effect=AgentUnavailable("daemon down"))
        client = TestClient(_make_app(p))
        r = client.put(
            "/api/v1/mcp/managed-remote-endpoints/safent-ads",
            json={"url": "https://ads.tenant.ts.net/mcp"},
        )
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "agent_unavailable"

    def test_empty_url_rejected_by_pydantic(self) -> None:
        p = _proxy()
        client = TestClient(_make_app(p))
        r = client.put("/api/v1/mcp/managed-remote-endpoints/safent-ads", json={"url": ""})
        assert r.status_code == 422
        p.call_mutator.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/v1/mcp/managed-remote/{slug}/connect
# ---------------------------------------------------------------------------


class TestConnectManagedRemote:
    def test_happy_path_sets_endpoint_then_adds_server(self) -> None:
        p = _proxy(
            mutator_side_effect=[
                {"ok": True},
                {"ok": True, "tool_count": 5},
            ]
        )
        client = TestClient(_make_app(p))
        r = client.post(
            "/api/v1/mcp/managed-remote/safent-ads/connect",
            json={"url": "https://ads.tenant.ts.net/mcp"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "tool_count": 5}
        assert p.call_mutator.call_count == 2

        first_call, second_call = p.call_mutator.call_args_list
        assert first_call.args == (
            "set_managed_remote_endpoint", "safent-ads", "https://ads.tenant.ts.net/mcp",
        )
        assert second_call.args[0] == "add_mcp_server"

    def test_add_mcp_server_draft_shape(self) -> None:
        import json as _json

        p = _proxy(
            mutator_side_effect=[
                {"ok": True},
                {"ok": True, "tool_count": 3},
            ]
        )
        client = TestClient(_make_app(p))
        client.post(
            "/api/v1/mcp/managed-remote/safent-ads/connect",
            json={"url": "https://ads.tenant.ts.net/mcp", "force": False},
        )
        _, second_call = p.call_mutator.call_args_list
        draft = _json.loads(second_call.args[1])
        assert draft["server_id"] == "safent-ads"
        assert draft["argv"] == ["npx", "-y", "mcp-remote@0.8.6", "https://ads.tenant.ts.net/mcp"]
        assert draft["label"] == "Safent Ads"
        assert draft["force"] is False

    def test_endpoint_rejection_short_circuits_before_add_returns_400(self) -> None:
        p = _proxy(mutator_return={"ok": False, "error": "managed_remote endpoint must use https://"})
        client = TestClient(_make_app(p))
        r = client.post(
            "/api/v1/mcp/managed-remote/safent-ads/connect",
            json={"url": "http://ads.example.com"},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["ok"] is False
        p.call_mutator.assert_called_once()

    def test_add_step_failure_returns_400(self) -> None:
        """Step 1 (set endpoint) succeeds; step 2 (add_mcp_server) rejects the
        draft (e.g. disallowed runner) — never a silent 200."""
        p = _proxy(
            mutator_side_effect=[
                {"ok": True},
                {"ok": False, "error": "runner 'curl' no permitido"},
            ]
        )
        client = TestClient(_make_app(p))
        r = client.post(
            "/api/v1/mcp/managed-remote/safent-ads/connect",
            json={"url": "https://ads.tenant.ts.net/mcp"},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["ok"] is False
        assert p.call_mutator.call_count == 2

    def test_503_on_agent_unavailable_during_set_endpoint(self) -> None:
        p = _proxy(mutator_side_effect=AgentUnavailable("daemon down"))
        client = TestClient(_make_app(p))
        r = client.post(
            "/api/v1/mcp/managed-remote/safent-ads/connect",
            json={"url": "https://ads.tenant.ts.net/mcp"},
        )
        assert r.status_code == 503
        p.call_mutator.assert_called_once()

    def test_503_on_agent_unavailable_during_add(self) -> None:
        p = _proxy(
            mutator_side_effect=[
                {"ok": True},
                AgentUnavailable("daemon down"),
            ]
        )
        client = TestClient(_make_app(p))
        r = client.post(
            "/api/v1/mcp/managed-remote/safent-ads/connect",
            json={"url": "https://ads.tenant.ts.net/mcp"},
        )
        assert r.status_code == 503
        assert p.call_mutator.call_count == 2

    def test_blocked_scan_result_returns_403(self) -> None:
        blocked = {
            "ok": False, "blocked": True, "scan_id": "abc123",
            "verdict": "WARN", "error": "revisión requerida",
        }
        p = _proxy(mutator_side_effect=[{"ok": True}, blocked])
        client = TestClient(_make_app(p))
        r = client.post(
            "/api/v1/mcp/managed-remote/safent-ads/connect",
            json={"url": "https://ads.tenant.ts.net/mcp"},
        )
        assert r.status_code == 403
        detail = r.json()["detail"]
        assert detail["blocked"] is True
        assert detail["scan_id"] == "abc123"
        assert detail["error"] == "revisión requerida"

    def test_empty_url_rejected_by_pydantic(self) -> None:
        p = _proxy()
        client = TestClient(_make_app(p))
        r = client.post("/api/v1/mcp/managed-remote/safent-ads/connect", json={"url": ""})
        assert r.status_code == 422
        p.call_mutator.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/v1/mcp (add_mcp_server) — same _raise_if_failed sweep
# ---------------------------------------------------------------------------


class TestAddMcpServer:
    def test_success_returns_201(self) -> None:
        p = _proxy(mutator_return={"ok": True, "tool_count": 2})
        client = TestClient(_make_app(p))
        r = client.post(
            "/api/v1/mcp",
            json={"server_id": "excel", "argv": ["npx", "-y", "excel-mcp-server"]},
        )
        assert r.status_code == 201
        assert r.json() == {"ok": True, "tool_count": 2}

    def test_validation_failure_returns_400(self) -> None:
        p = _proxy(mutator_return={
            "ok": False, "error": "runner 'curl' no permitido (allowlist: npx, uvx)",
        })
        client = TestClient(_make_app(p))
        r = client.post(
            "/api/v1/mcp",
            json={"server_id": "evil", "argv": ["curl", "http://x"]},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["ok"] is False

    def test_blocked_scan_result_returns_403(self) -> None:
        p = _proxy(mutator_return={
            "ok": False, "blocked": True, "scan_id": "s1", "verdict": "FAIL",
            "error": "paquete con firma maliciosa conocida",
        })
        client = TestClient(_make_app(p))
        r = client.post(
            "/api/v1/mcp",
            json={"server_id": "bad-pkg", "argv": ["npx", "-y", "bad-pkg"]},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["blocked"] is True
