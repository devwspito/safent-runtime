"""Read and change account models only through the owner's existing connection."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hermes.shell_server.cowork.native_models_api import create_native_models_router
from hermes.tasks.control_plane.domain.ports import AgentUnavailable


@pytest.fixture
def app():
    app = FastAPI()
    app.state.shell_webui_token = "owner-session"
    app.state.dbus_proxy = AsyncMock()
    app.state.dbus_proxy.call_dict.return_value = {
        "provider_id": "openai-codex",
        "active_model": "one",
        "models": ["one", "two"],
        "token": "must-not-return",
    }
    app.state.dbus_proxy.call_mutator.return_value = {
        "provider_id": "openai-codex",
        "active_model": "two",
        "token": "must-not-return",
    }
    app.include_router(create_native_models_router(), prefix="/api/v1/providers")
    return app


async def request(app, method="GET", body=None, token="owner-session"):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(
            method,
            "/api/v1/providers/native/models"
            if method == "GET"
            else "/api/v1/providers/native/model",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )


@pytest.mark.asyncio
async def test_catalog_exposes_only_safe_fields(app):
    result = await request(app)
    assert result.status_code == 200
    assert result.json() == {
        "provider_id": "openai-codex",
        "active_model": "one",
        "models": ["one", "two"],
    }
    app.state.dbus_proxy.call_dict.assert_awaited_once_with(
        "list_native_provider_models", "openai-codex"
    )


@pytest.mark.asyncio
async def test_model_write_uses_existing_native_connection_only(app):
    result = await request(
        app, "PATCH", {"provider_id": "openai-codex", "model": "two", "expected_model": "one"}
    )
    assert result.json() == {"provider_id": "openai-codex", "active_model": "two"}
    app.state.dbus_proxy.call_mutator.assert_awaited_once_with(
        "set_native_provider_model", "openai-codex", "two", "one"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PATCH"])
async def test_internal_or_missing_session_cannot_select_or_discover(app, method):
    result = await request(
        app,
        method,
        {"provider_id": "openai-codex", "model": "two", "expected_model": "one"},
        token="internal-token",
    )
    assert result.status_code == 403
    app.state.dbus_proxy.call_dict.assert_not_awaited()
    app.state.dbus_proxy.call_mutator.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,status",
    [
        ("managed_by_enterprise", 403),
        ("selection_changed", 409),
        ("model_not_available", 400),
        ("private-error", 503),
    ],
)
async def test_typed_failures_never_echo_unknown_daemon_errors(app, code, status):
    app.state.dbus_proxy.call_dict.return_value = {"code": code}
    result = await request(app)
    assert result.status_code == status
    assert "private-error" not in result.text


@pytest.mark.asyncio
async def test_daemon_unavailable_is_not_an_empty_account_catalog(app):
    app.state.dbus_proxy.call_dict.side_effect = AgentUnavailable("private-token")
    result = await request(app)
    assert result.status_code == 503
    assert "private-token" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [{"api_key": "unexpected"}, {"base_url": "https://other.invalid"}, {"set_active": True}],
)
async def test_no_credential_endpoint_or_activation_changes_allowed(app, extra):
    result = await request(
        app,
        "PATCH",
        {"provider_id": "openai-codex", "model": "two", "expected_model": "one", **extra},
    )
    assert result.status_code == 422
    app.state.dbus_proxy.call_mutator.assert_not_awaited()
