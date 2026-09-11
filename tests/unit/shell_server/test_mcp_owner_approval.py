"""A force flag cannot mint authority or change the owner-reviewed MCP operation."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.cowork.mcp_api import create_mcp_router
from hermes.shell_server.cowork.security_api import create_security_router

pytestmark = pytest.mark.unit
_DRAFT = {"server_id": "example", "argv": ["npx", "-y", "example@1"], "env": {"KEY": "secret"}}
_MANAGED = {"url": "https://ads.example.com/mcp"}


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.state.shell_webui_token = "owner-ui"
    app.state.dbus_proxy = MagicMock(call_mutator=AsyncMock(return_value={"ok": True}))
    app.include_router(create_security_router())
    app.include_router(create_mcp_router())
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"Authorization": "Bearer owner-ui"})


def _approve(client: TestClient, managed: bool = False):
    intent = (
        {"operation": "managed_remote", "slug": "safent-ads", **_MANAGED}
        if managed else {"operation": "add", **_DRAFT}
    )
    return client.post("/api/v1/security/decisions", json={
        "scan_id": "scan-1", "kind": "mcp", "identifier": "example@1",
        "decision": "approve", "mcp_approval": intent,
    })


def _install(client: TestClient, grant: str = "", *, managed: bool = False, patch=None):
    return client.post(
        "/api/v1/mcp/managed-remote/safent-ads/connect" if managed else "/api/v1/mcp",
        json={**(_MANAGED if managed else _DRAFT), "force": True, **(patch or {})},
        headers={"X-Owner-Approval-Grant": grant},
    )


@pytest.mark.parametrize("managed", [False, True])
def test_force_without_grant_never_mutates_endpoint_or_server(client, app, managed):
    assert _install(client, managed=managed).status_code == 401
    app.state.dbus_proxy.call_mutator.assert_not_called()


@pytest.mark.parametrize("managed", [False, True])
def test_exact_approval_is_one_use(client, app, managed):
    approval = _approve(client, managed)
    assert approval.status_code == 201
    grant = approval.json()["approval_grant"]
    assert "secret" not in grant
    assert _install(client, grant, managed=managed).status_code in (200, 201)
    app.state.dbus_proxy.call_mutator.reset_mock()
    assert _install(client, grant, managed=managed).status_code == 401
    app.state.dbus_proxy.call_mutator.assert_not_called()


@pytest.mark.parametrize("patch", [
    {"server_id": "different"}, {"argv": ["npx", "malicious"]},
    {"env": {"KEY": "different-secret"}}, {"label": "different label"},
])
def test_changing_any_reviewed_draft_field_burns_grant(client, app, patch):
    grant = _approve(client).json()["approval_grant"]
    app.state.dbus_proxy.call_mutator.reset_mock()
    assert _install(client, grant, patch=patch).status_code == 401
    assert _install(client, grant).status_code == 401
    app.state.dbus_proxy.call_mutator.assert_not_called()


def test_changing_url_is_rejected_before_endpoint_write(client, app):
    grant = _approve(client, True).json()["approval_grant"]
    app.state.dbus_proxy.call_mutator.reset_mock()
    response = _install(client, grant, managed=True, patch={"url": "https://other.example/mcp"})
    assert response.status_code == 401
    app.state.dbus_proxy.call_mutator.assert_not_called()


def test_cross_operation_grant_is_rejected(client, app):
    grant = _approve(client).json()["approval_grant"]
    app.state.dbus_proxy.call_mutator.reset_mock()
    assert _install(client, grant, managed=True).status_code == 401
    app.state.dbus_proxy.call_mutator.assert_not_called()


def test_internal_daemon_cannot_mint_or_use_owner_grant(client, app):
    grant = _approve(client).json()["approval_grant"]
    app.state.dbus_proxy.call_mutator.reset_mock()
    client.headers["Authorization"] = "Bearer internal-daemon"
    assert _approve(client).status_code == 403
    assert _install(client, grant).status_code == 403
    app.state.dbus_proxy.call_mutator.assert_not_called()


def test_failed_decision_cannot_authorize_force(client, app):
    app.state.dbus_proxy.call_mutator.return_value = {"ok": False}
    response = _approve(client)
    assert response.status_code == 502
    assert "approval_grant" not in response.json()


def test_legacy_approval_without_reviewed_draft_is_rejected(client, app):
    response = client.post("/api/v1/security/decisions", json={
        "scan_id": "scan-1", "kind": "mcp", "identifier": "example@1", "decision": "approve",
    })
    assert response.status_code == 422
    app.state.dbus_proxy.call_mutator.assert_not_called()


def test_normal_install_keeps_daemon_scan(client, app):
    assert client.post("/api/v1/mcp", json=_DRAFT).status_code == 201
    app.state.dbus_proxy.call_mutator.assert_awaited_once()
