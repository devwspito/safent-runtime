"""Unit tests for the inbound-delegation REST API (FASE 3, A2A cross-human).

Coverage:
  - GET  /api/v1/inbound-delegations           — list, fail-soft on daemon unavailable.
  - POST /api/v1/inbound-delegations/{message_id} — resolve (approve/reject),
    fail-hard 503 on daemon unavailable.
  - The resolve verb call never carries an operator identity from the request
    body (only message_id + decision) — provenance is derived D-Bus-side.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.cowork.inbound_delegations_api import (
    create_inbound_delegations_router,
)
from hermes.tasks.control_plane.domain.ports import AgentUnavailable

pytestmark = pytest.mark.unit


def _make_app(proxy: MagicMock) -> FastAPI:
    app = FastAPI()
    app.state.dbus_proxy = proxy
    app.include_router(create_inbound_delegations_router())
    return app


def _proxy(*, list_return=None, dict_return=None) -> MagicMock:
    p = MagicMock()
    p.call_list = AsyncMock(return_value=list_return if list_return is not None else [])
    p.call_dict = AsyncMock(return_value=dict_return if dict_return is not None else {})
    return p


class TestListPendingInboundDelegations:
    def test_returns_list(self) -> None:
        pending = [
            {
                "message_id": "msg-1",
                "from_employee_id": "alice@org.example",
                "body": "please review this",
                "issued_at": "2026-07-04T00:00:00+00:00",
                "created_at": "2026-07-04T00:00:01+00:00",
            }
        ]
        client = TestClient(_make_app(_proxy(list_return=pending)))

        r = client.get("/api/v1/inbound-delegations")

        assert r.status_code == 200
        assert r.json() == pending

    def test_fail_soft_returns_empty_list_on_unavailable(self) -> None:
        p = MagicMock()
        p.call_list = AsyncMock(side_effect=AgentUnavailable("daemon down"))
        client = TestClient(_make_app(p))

        r = client.get("/api/v1/inbound-delegations")

        assert r.status_code == 200
        assert r.json() == []

    def test_uses_list_pending_delegations_verb(self) -> None:
        p = _proxy(list_return=[])
        client = TestClient(_make_app(p))

        client.get("/api/v1/inbound-delegations")

        p.call_list.assert_called_once_with("list_pending_delegations")


class TestResolveInboundDelegation:
    def test_approve_returns_ok_and_task_id(self) -> None:
        p = _proxy(dict_return={"ok": True, "task_id": "task-123"})
        client = TestClient(_make_app(p))

        r = client.post(
            "/api/v1/inbound-delegations/msg-1", json={"decision": "approve"}
        )

        assert r.status_code == 200
        assert r.json() == {"ok": True, "task_id": "task-123"}

    def test_reject_returns_ok(self) -> None:
        p = _proxy(dict_return={"ok": True})
        client = TestClient(_make_app(p))

        r = client.post(
            "/api/v1/inbound-delegations/msg-1", json={"decision": "reject"}
        )

        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_invalid_decision_is_rejected_by_schema(self) -> None:
        p = _proxy()
        client = TestClient(_make_app(p))

        r = client.post(
            "/api/v1/inbound-delegations/msg-1", json={"decision": "maybe"}
        )

        assert r.status_code == 422
        p.call_dict.assert_not_called()

    def test_503_on_agent_unavailable(self) -> None:
        p = MagicMock()
        p.call_dict = AsyncMock(side_effect=AgentUnavailable("daemon down"))
        client = TestClient(_make_app(p))

        r = client.post(
            "/api/v1/inbound-delegations/msg-1", json={"decision": "approve"}
        )

        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "agent_unavailable"

    def test_resolve_call_carries_no_operator_identity_from_request_body(self) -> None:
        """Provenance guarantee (CWE-862): only message_id + decision travel
        from the HTTP request — the resolver identity is derived D-Bus-side
        from the authenticated channel, never from this payload."""
        p = _proxy(dict_return={"ok": True, "task_id": "task-1"})
        client = TestClient(_make_app(p))

        client.post("/api/v1/inbound-delegations/msg-42", json={"decision": "approve"})

        p.call_dict.assert_called_once_with(
            "resolve_inbound_delegation", "msg-42", "approve", ""
        )
