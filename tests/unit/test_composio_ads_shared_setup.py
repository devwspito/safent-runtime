from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("composio.exceptions")
from hermes.integrations.composio.composio_client import ComposioApiError, ComposioClient
from hermes.shell_server.integrations import ads_setup, api
from hermes.shell_server.integrations.repo import SQLiteIntegrationsRepository
from hermes.shell_server.security import secrets as secrets_module
from hermes.shell_server.security.secrets import SecretsVault


@pytest.fixture
def configured(tmp_path, monkeypatch):
    master = tmp_path / "master.key"
    master.write_bytes(b"\xa8" * 32)
    monkeypatch.setattr(secrets_module, "_MASTER_KEY_PATH", master)
    db = tmp_path / "integrations.db"
    repo = SQLiteIntegrationsRepository(db_path=db, vault=SecretsVault())
    repo.set_credential(kind="composio", api_key="test-project-key", entity_id="test-install")
    result = NS(id="ac_google", toolkit_slug="googleads", status="ENABLED")
    fake = NS(resolve_ads_auth_config=AsyncMock(return_value=result))
    return db, repo, Mock(return_value=fake), fake


@pytest.mark.asyncio
async def test_prepares_only_google_and_reuses_stored_result(configured):
    db, repo, factory, fake = configured
    assert await ads_setup.prepare_composio_ads_configs(db, client_factory=factory) == {
        "googleads": True,
        "metaads": False,
    }
    assert repo.auth_config_ids() == {"googleads": "ac_google"}
    fake.resolve_ads_auth_config.assert_awaited_once_with("googleads")
    factory.assert_called_once_with("test-project-key", auth_config_ids={})
    await ads_setup.prepare_composio_ads_configs(db, client_factory=factory)
    fake.resolve_ads_auth_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_meta_config_preserved_not_discovered_from_project(configured):
    db, repo, factory, fake = configured
    repo.set_auth_config(toolkit_slug="metaads", auth_config_id="ac_owner_meta")
    assert await ads_setup.prepare_composio_ads_configs(db, client_factory=factory) == {
        "googleads": True,
        "metaads": True,
    }
    assert repo.auth_config_ids()["metaads"] == "ac_owner_meta"
    fake.resolve_ads_auth_config.assert_awaited_once_with("googleads")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ComposioApiError(403, "private-error"), TimeoutError()])
async def test_provider_failure_does_not_fake_ready_or_persist(configured, error):
    db, repo, factory, fake = configured
    fake.resolve_ads_auth_config.side_effect = error
    assert await ads_setup.prepare_composio_ads_configs(db, client_factory=factory) == {
        "googleads": False,
        "metaads": False,
    }
    assert repo.auth_config_ids() == {}


@pytest.mark.asyncio
async def test_rotation_during_network_request_does_not_attach_old_config(configured):
    db, repo, factory, fake = configured

    async def rotate(_slug):
        repo.set_credential(kind="composio", api_key="new-project", entity_id="test-install")
        return NS(id="ac_previous_project", toolkit_slug="googleads", status="ENABLED")

    fake.resolve_ads_auth_config.side_effect = rotate
    assert not (await ads_setup.prepare_composio_ads_configs(db, client_factory=factory))[
        "googleads"
    ]
    assert repo.auth_config_ids() == {}


@pytest.mark.asyncio
async def test_disabled_integration_never_calls_provider(configured):
    db, repo, factory, _ = configured
    repo.set_credential(kind="composio", api_key="test-project-key", enabled=False)
    assert await ads_setup.prepare_composio_ads_configs(db, client_factory=factory) == {
        "googleads": False,
        "metaads": False,
    }
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_platform_result_is_not_persisted(configured):
    db, repo, factory, fake = configured
    fake.resolve_ads_auth_config.return_value = NS(
        id="ac_wrong", toolkit_slug="gmail", status="ENABLED"
    )
    assert not (await ads_setup.prepare_composio_ads_configs(db, client_factory=factory))[
        "googleads"
    ]
    assert repo.auth_config_ids() == {}


@pytest.mark.asyncio
async def test_sdk_resolves_managed_config_without_creating_connection():
    sdk = Mock()
    sdk.toolkits.get.return_value = NS(
        slug="googleads", enabled=True, composio_managed_auth_schemes=["OAUTH2"]
    )
    sdk.auth_configs.list.return_value = NS(items=[NS(id="ac_google", status="ENABLED")])
    sdk.auth_configs.get.return_value = NS(
        id="ac_google",
        toolkit=NS(slug="googleads"),
        status="ENABLED",
        auth_scheme="OAUTH2",
        credentials={"secret": "never-export"},
    )
    client = ComposioClient("test", sdk=sdk)
    result = await client.resolve_ads_auth_config("googleads")
    assert result.id == "ac_google"
    assert "never-export" not in repr(result)
    sdk.connected_accounts.link.assert_not_called()
    sdk.auth_configs.create.assert_not_called()


@pytest.mark.asyncio
async def test_sdk_does_not_invent_managed_meta_app():
    sdk = Mock()
    sdk.toolkits.get.return_value = NS(
        slug="metaads", enabled=True, composio_managed_auth_schemes=[]
    )
    with pytest.raises(ComposioApiError):
        await ComposioClient("test", sdk=sdk).resolve_ads_auth_config("metaads")
    sdk.auth_configs.create.assert_not_called()
    sdk.connected_accounts.link.assert_not_called()


def test_preparation_is_owner_only_and_never_returns_credentials(configured, monkeypatch):
    db, _, _, _ = configured
    prepare = AsyncMock(return_value={"googleads": True, "metaads": False})
    monkeypatch.setattr(ads_setup, "prepare_composio_ads_configs", prepare)
    app = FastAPI()
    app.state.shell_webui_token = "owner-session"
    app.state.shell_auth_token = "agent-session"
    app.include_router(api.create_integrations_router(db))
    client = TestClient(app)
    path = "/api/v1/integrations/composio/ads/prepare"
    for bearer in ("", "agent-session", "foreign-session"):
        assert client.post(path, headers={"Authorization": f"Bearer {bearer}"}).status_code == 403
    prepare.assert_not_awaited()
    result = client.post(path, headers={"Authorization": "Bearer owner-session"})
    assert result.json() == {"googleads": True, "metaads": False}
    assert "key" not in result.text


def test_key_rotation_immediately_requests_fresh_private_ads_lease(configured):
    db, _, _, _ = configured
    app = FastAPI()
    app.state.shell_webui_token = "owner-session"
    refresh = NS(ensure=AsyncMock())
    app.state.composio_lease_refresh = refresh
    app.include_router(api.create_integrations_router(db))
    response = TestClient(app).post(
        "/api/v1/integrations/composio/key",
        json={"api_key": "rotated-key"},
        headers={"Authorization": "Bearer owner-session"},
    )
    assert response.status_code == 200
    assert "rotated-key" not in response.text
    refresh.ensure.assert_awaited_once_with(force=True)
