"""Community brake: stopping is immediate, only the UI owner may release it."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.cowork.security_api import create_security_router
from hermes.tasks.control_plane.domain.ports import AgentUnavailable

pytestmark = pytest.mark.unit


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.state.shell_webui_token = "test-owner-ui"
    app.state.shell_auth_token = "test-internal-daemon"
    app.state.dbus_proxy = MagicMock(
        call_bool=AsyncMock(return_value=True),
        call_dict=AsyncMock(return_value={"engaged": True, "reason": "freno"}),
    )
    app.include_router(create_security_router())
    return app


def test_engage_needs_no_additional_factor(app: FastAPI) -> None:
    client = TestClient(app, headers={"Authorization": "Bearer test-internal-daemon"})
    response = client.post(
        "/api/v1/security/kill-switch", json={"engaged": True, "reason": "stop"},
    )
    assert response.status_code == 200
    app.state.dbus_proxy.call_bool.assert_awaited_once_with("pause", "stop")


def test_owner_releases_without_mfa_or_device_password(app: FastAPI) -> None:
    client = TestClient(app, headers={"Authorization": "Bearer test-owner-ui"})
    response = client.post("/api/v1/security/kill-switch", json={"engaged": False})
    assert response.status_code == 200
    app.state.dbus_proxy.call_bool.assert_awaited_once_with("resume", "api")


@pytest.mark.parametrize("token", ["", "test-internal-daemon", "another-session"])
def test_non_owner_cannot_release(app: FastAPI, token: str) -> None:
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    response = client.post("/api/v1/security/kill-switch", json={"engaged": False})
    assert response.status_code == 403
    app.state.dbus_proxy.call_bool.assert_not_called()


@pytest.mark.parametrize("engaged", [True, False])
def test_unavailable_daemon_is_not_success(app: FastAPI, engaged: bool) -> None:
    app.state.dbus_proxy.call_bool.side_effect = AgentUnavailable("offline")
    client = TestClient(app, headers={"Authorization": "Bearer test-owner-ui"})
    response = client.post("/api/v1/security/kill-switch", json={"engaged": engaged})
    assert response.status_code == 503


def test_get_reflects_persisted_daemon_state(app: FastAPI) -> None:
    response = TestClient(app).get("/api/v1/security/kill-switch")
    assert response.status_code == 200
    assert response.json() == {"engaged": True, "reason": "freno"}


@pytest.mark.parametrize("state", [None, {}, {"engaged": "false"}, {"engaged": 0}])
def test_invalid_state_is_unknown_not_released(app: FastAPI, state: object) -> None:
    app.state.dbus_proxy.call_dict.return_value = state
    response = TestClient(app).get("/api/v1/security/kill-switch")
    assert response.status_code == 503
    assert "engaged" not in response.json()


def test_unavailable_state_is_unknown_not_released(app: FastAPI) -> None:
    app.state.dbus_proxy.call_dict.side_effect = AgentUnavailable("offline")
    response = TestClient(app).get("/api/v1/security/kill-switch")
    assert response.status_code == 503
    assert "engaged" not in response.json()
