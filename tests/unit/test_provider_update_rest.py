"""Repair saved providers through the same governed D-Bus write path."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hermes.shell_server.cowork.providers_api import create_providers_router
from hermes.tasks.control_plane.domain.ports import AgentUnavailable

PID = "12345678-1234-4234-8234-123456789abc"


@pytest.fixture
def app():
    app = FastAPI()
    app.state.dbus_proxy = AsyncMock()
    app.state.dbus_proxy.call_list.return_value = [{"provider_id": PID, "alias": "mine"}]
    app.state.dbus_proxy.call_mutator.return_value = {"provider_id": PID}
    app.include_router(create_providers_router())
    return app


@pytest.mark.asyncio
async def test_patch_preserves_key_and_uses_governed_mutator(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            f"/api/v1/providers/{PID}", json={"base_url": "https://model.example/v1"}
        )
    assert response.status_code == 200
    name, pid, draft = app.state.dbus_proxy.call_mutator.call_args.args
    assert (name, pid) == ("update_provider", PID)
    assert json.loads(draft) == {"base_url": "https://model.example/v1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [{"managed_by": "cloud"}, {"set_active": True}, {"kind": "openai"}, {"default_model": ""}],
)
async def test_patch_rejects_governance_activation_and_empty_model(app, extra):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(f"/api/v1/providers/{PID}", json=extra)
    assert response.status_code == 422
    app.state.dbus_proxy.call_mutator.assert_not_called()


@pytest.mark.asyncio
async def test_patch_cannot_modify_corporate_provider(app):
    app.state.dbus_proxy.call_list.return_value = [{"provider_id": PID, "managed_by": "cloud"}]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(f"/api/v1/providers/{PID}", json={"api_key": "fake"})
    assert response.status_code == 403
    app.state.dbus_proxy.call_mutator.assert_not_called()


@pytest.mark.asyncio
async def test_patch_fails_closed_when_management_cannot_be_checked(app):
    app.state.dbus_proxy.call_list.side_effect = AgentUnavailable("private error")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(f"/api/v1/providers/{PID}", json={"api_key": "fake"})
    assert response.status_code == 503
    assert "private error" not in response.text
    app.state.dbus_proxy.call_mutator.assert_not_called()


@pytest.mark.asyncio
async def test_native_selection_unavailable_is_not_reported_as_no_model(app):
    app.state.dbus_proxy.call_dict.side_effect = AgentUnavailable("private daemon error")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/providers/native/active")
    assert response.status_code == 503
    assert "private daemon error" not in response.text
