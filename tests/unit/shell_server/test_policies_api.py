"""Owner-only policy changes: no MFA, no internal-daemon bypass or partial batch."""
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.capabilities.tool_policy import ToolPolicyStore
from hermes.shell_server.cowork.policies_api import create_policies_router

pytestmark = pytest.mark.unit
_MUTATIONS = [
    ("preset", {"preset": "permisivo"}),
    ("tool", {"tool": "web_search", "enabled": False}),
    ("tools", {"tools": {"web_search": False, "read_file": False}}),
    ("approval_on_dangers", {"enabled": False}),
]


def _app(tmp_path: Path) -> tuple[FastAPI, ToolPolicyStore]:
    store = ToolPolicyStore(tmp_path / "policy.json")
    app = FastAPI()
    app.state.shell_webui_token = "owner-ui"
    app.state.shell_auth_token = "internal-daemon"
    app.include_router(create_policies_router(policy=store))
    return app, store


@pytest.mark.parametrize(("path", "payload"), _MUTATIONS)
@pytest.mark.parametrize("approval_enabled", [True, False])
def test_owner_can_change_policy_without_mfa(
    tmp_path: Path, path: str, payload: dict, approval_enabled: bool,
) -> None:
    app, store = _app(tmp_path)
    store.set_approval_on_dangers(approval_enabled)
    client = TestClient(app, headers={"Authorization": "Bearer owner-ui"})
    assert client.post(f"/api/v1/policies/{path}", json=payload).status_code == 200


@pytest.mark.parametrize(("path", "payload"), _MUTATIONS)
@pytest.mark.parametrize("token", ["", "internal-daemon", "another-owner"])
@pytest.mark.parametrize("approval_enabled", [True, False])
def test_non_owner_cannot_change_policy_in_any_posture(
    tmp_path: Path, path: str, payload: dict, token: str, approval_enabled: bool,
) -> None:
    app, store = _app(tmp_path)
    store.set_approval_on_dangers(approval_enabled)
    before = (tmp_path / "policy.json").read_bytes()
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    assert client.post(f"/api/v1/policies/{path}", json=payload).status_code == 403
    assert (tmp_path / "policy.json").read_bytes() == before


def test_batch_is_persisted_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store = _app(tmp_path)
    save = Mock(wraps=store._save)
    monkeypatch.setattr(store, "_save", save)
    client = TestClient(app, headers={"Authorization": "Bearer owner-ui"})
    response = client.post(
        "/api/v1/policies/tools", json={"tools": {"web_search": False, "read_file": False}},
    )
    assert response.status_code == 200
    assert save.call_count == 1
    assert store.is_owner_disabled("web_search")
    assert store.is_owner_disabled("read_file")


@pytest.mark.parametrize("payload", [
    {"tools": {"web_search": False, "read_file": "false"}},
    {"tools": {"web_search": 1}},
    {"tools": {}},
    {"tools": {"web_search": False}, "totp": "123456"},
])
def test_invalid_batch_has_no_partial_side_effect(tmp_path: Path, payload: dict) -> None:
    app, _store = _app(tmp_path)
    client = TestClient(app, headers={"Authorization": "Bearer owner-ui"})
    assert client.post("/api/v1/policies/tools", json=payload).status_code == 422
    assert not (tmp_path / "policy.json").exists()


def test_legacy_mfa_endpoint_is_not_exposed(tmp_path: Path) -> None:
    app, store = _app(tmp_path)
    client = TestClient(app, headers={"Authorization": "Bearer owner-ui"})
    response = client.post("/api/v1/policies/mfa_on_dangers", json={"enabled": False})
    assert response.status_code == 404
    assert "mfa_on_dangers" not in store.snapshot()
