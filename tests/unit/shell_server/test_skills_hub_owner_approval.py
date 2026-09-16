"""Community installs retain explicit, one-use owner approval without MFA."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hermes.shell_server.cowork.security_api import create_security_router
from hermes.shell_server.cowork.skills_api import create_skills_hub_router
from hermes.shell_server.security import owner_confirmation as confirmation

pytestmark = pytest.mark.unit
_OWNER = "test-owner-ui-session"
_DECISION = {
    "scan_id": "scan-1", "decision": "allow_once", "kind": "skill", "identifier": "skill-a",
}


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.state.shell_webui_token = _OWNER
    app.state.shell_auth_token = "test-internal-daemon-session"
    app.state.dbus_proxy = MagicMock(call_mutator=AsyncMock(return_value={"ok": True}))
    app.include_router(create_security_router())
    app.include_router(create_skills_hub_router(tmp_path / "skills.db"))
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {_OWNER}"})


def _approve(client: TestClient) -> str:
    response = client.post("/api/v1/security/decisions", json=_DECISION)
    assert response.status_code == 201, response.text
    return response.json()["approval_grant"]


def _install(client: TestClient, grant: str, *, identifier: str = "skill-a"):
    return client.post(
        "/api/v1/skills/hub/install", json={"identifier": identifier, "force": True},
        headers={"X-Owner-Approval-Grant": grant},
    )


def test_owner_can_confirm_and_install_without_mfa(client: TestClient, app: FastAPI) -> None:
    assert _install(client, _approve(client)).status_code == 202
    app.state.dbus_proxy.call_mutator.assert_awaited_with("install_hub_skill", "skill-a", True)


def test_normal_install_keeps_the_daemon_scan_path(client: TestClient, app: FastAPI) -> None:
    response = client.post("/api/v1/skills/hub/install", json={"identifier": "skill-a"})
    assert response.status_code == 202
    app.state.dbus_proxy.call_mutator.assert_awaited_with("install_hub_skill", "skill-a", False)


def test_old_totp_payload_is_not_a_compatibility_bypass(client: TestClient, app: FastAPI) -> None:
    response = client.post(
        "/api/v1/skills/hub/install",
        json={"identifier": "skill-a", "force": True, "totp": "123456"},
    )
    assert response.status_code == 422
    app.state.dbus_proxy.call_mutator.assert_not_called()


def test_approval_is_single_use(client: TestClient) -> None:
    grant = _approve(client)
    assert _install(client, grant).status_code == 202
    assert _install(client, grant).status_code == 401


def test_wrong_target_burns_approval(client: TestClient, app: FastAPI) -> None:
    grant = _approve(client)
    app.state.dbus_proxy.call_mutator.reset_mock()
    assert _install(client, grant, identifier="skill-b").status_code == 401
    assert _install(client, grant).status_code == 401
    app.state.dbus_proxy.call_mutator.assert_not_called()


@pytest.mark.parametrize("grant", ["", "made-up"])
def test_force_requires_prior_confirmation(client: TestClient, app: FastAPI, grant: str) -> None:
    assert _install(client, grant).status_code == 401
    app.state.dbus_proxy.call_mutator.assert_not_called()


@pytest.mark.parametrize("token", ["", "test-internal-daemon-session", "another-owner"])
def test_daemon_or_unauthenticated_caller_cannot_approve(app: FastAPI, token: str) -> None:
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    assert client.post("/api/v1/security/decisions", json=_DECISION).status_code == 403
    assert _install(client, "some-grant").status_code == 403
    app.state.dbus_proxy.call_mutator.assert_not_called()


@pytest.mark.parametrize("elapsed", [120, 121])
def test_expiry_is_inclusive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, elapsed: int,
) -> None:
    monkeypatch.setattr(confirmation.time, "monotonic", lambda: 1000)
    grant = _approve(client)
    monkeypatch.setattr(confirmation.time, "monotonic", lambda: 1000 + elapsed)
    assert _install(client, grant).status_code == 401


def test_rotating_owner_session_invalidates_pending_confirmation(
    client: TestClient, app: FastAPI,
) -> None:
    grant = _approve(client)
    app.state.shell_webui_token = "new-owner-ui-session"
    client.headers["Authorization"] = "Bearer new-owner-ui-session"
    assert _install(client, grant).status_code == 401


def test_confirmation_does_not_cross_application_instances(client: TestClient) -> None:
    grant = _approve(client)
    another = FastAPI()
    another.state.shell_webui_token = _OWNER
    another.include_router(create_skills_hub_router(Path("/unused-test-skills.db")))
    isolated_client = TestClient(another, headers={"Authorization": f"Bearer {_OWNER}"})
    assert _install(isolated_client, grant).status_code == 401


@pytest.mark.parametrize("result", [{"ok": False}, {}, None])
def test_failed_decision_never_yields_an_approval(
    client: TestClient, app: FastAPI, result: object,
) -> None:
    app.state.dbus_proxy.call_mutator.return_value = result
    response = client.post("/api/v1/security/decisions", json=_DECISION)
    assert response.status_code == 502
    assert not getattr(app.state, "owner_approval_grants", {})


@pytest.mark.parametrize("changes", [{"kind": "rpm"}, {"decision": "deny"}])
def test_non_skill_and_denied_decisions_yield_no_grant(client: TestClient, changes: dict) -> None:
    response = client.post("/api/v1/security/decisions", json={**_DECISION, **changes})
    assert response.status_code == 201
    assert "approval_grant" not in response.json()


def test_parallel_consumers_have_one_winner(client: TestClient, app: FastAPI) -> None:
    grant = _approve(client)
    request = Request({
        "type": "http", "app": app,
        "headers": [(b"authorization", f"Bearer {_OWNER}".encode()),
                    (b"x-owner-approval-grant", grant.encode())],
    })

    def consume() -> bool:
        try:
            confirmation.require_owner_approval(
                request, identifier="skill-a", action="install_hub_skill"
            )
            return True
        except HTTPException:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: consume(), range(16))) == 1
