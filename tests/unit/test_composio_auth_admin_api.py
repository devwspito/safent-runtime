"""Owner-selected Composio configuration and connection isolation at the API."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("composio.exceptions")
from hermes.integrations.composio.composio_client import ComposioApiError
from hermes.shell_server.integrations import api
from hermes.shell_server.integrations.repo import SQLiteIntegrationsRepository
from hermes.shell_server.security import secrets as secrets_module
from hermes.shell_server.security.secrets import SecretsVault

OWNER = "owner-ui-session"
BASE = "/api/v1/integrations/composio"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    key_path = tmp_path / "master.key"
    key_path.write_bytes(b"\xaa" * 32)
    monkeypatch.setattr(secrets_module, "_MASTER_KEY_PATH", key_path)
    db = tmp_path / "integrations.db"
    app = FastAPI()
    app.state.shell_webui_token = OWNER
    app.state.shell_auth_token = "daemon-session"
    app.include_router(api.create_integrations_router(db))
    repo = SQLiteIntegrationsRepository(db_path=db, vault=SecretsVault())
    repo.set_credential(kind="composio", api_key="fake-key", entity_id="server-owned-entity")
    fake = NS(
        prepare_meta_auth_config=AsyncMock(return_value=NS(id="ac-meta-setup")),
        validate_auth_config=AsyncMock(),
        initiate_connection=AsyncMock(
            return_value=NS(
                connected_account_id="ca-new",
                redirect_url="https://connect.example",
                status="INITIATED",
            )
        ),
        delete_connection=AsyncMock(),
        get_connected_account=AsyncMock(
            return_value=NS(
                id="ca-owned",
                toolkit_slug="metaads",
                entity_id="server-owned-entity",
                status="ACTIVE",
                auth_config_id="ac-owned",
            )
        ),
    )
    monkeypatch.setattr(api, "_build_client", lambda _repo: fake)
    return TestClient(app, headers={"Authorization": f"Bearer {OWNER}"}), repo, fake


def test_owner_selects_validated_non_secret_configuration(setup):
    client, repo, fake = setup
    response = client.put(f"{BASE}/auth-configs/metaads", json={"auth_config_id": "ac-owned"})
    assert response.status_code == 200
    assert response.json() == {"toolkit_slug": "metaads", "auth_config_id": "ac-owned"}
    fake.validate_auth_config.assert_awaited_once_with("metaads", "ac-owned")
    assert repo.auth_config_ids() == {"metaads": "ac-owned"}
    assert client.get(f"{BASE}/auth-configs/metaads").json() == response.json()
    assert client.delete(f"{BASE}/auth-configs/metaads").json()["auth_config_id"] is None
    assert repo.auth_config_ids() == {}


def test_rejected_configuration_is_not_persisted_or_leaked(setup):
    client, repo, fake = setup
    fake.validate_auth_config.side_effect = ComposioApiError(403, "NEVER-EXPOSE-THIS-SECRET")
    response = client.put(f"{BASE}/auth-configs/metaads", json={"auth_config_id": "ac-foreign"})
    assert response.status_code == 409
    assert "NEVER-EXPOSE" not in response.text
    assert repo.auth_config_ids() == {}


@pytest.mark.parametrize("token", ["", "daemon-session", "different-owner"])
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("put", "/auth-configs/metaads", {"auth_config_id": "ac-owned"}),
        ("get", "/auth-configs/metaads", None),
        ("delete", "/auth-configs/metaads", None),
        ("post", "/connect", {"toolkit_slug": "metaads"}),
        ("post", "/key", {"api_key": "replacement"}),
        ("post", "/meta/setup", {"client_id": "123456789", "client_secret": "fake-secret"}),
        ("delete", "/connected/ca-owned", None),
    ],
)
def test_daemon_cannot_mutate_owner_connections(setup, token, method, path, body):
    client, repo, fake = setup
    response = client.request(
        method, BASE + path, json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    fake.validate_auth_config.assert_not_called()
    fake.initiate_connection.assert_not_called()
    fake.delete_connection.assert_not_called()
    fake.prepare_meta_auth_config.assert_not_called()
    assert repo.reveal_api_key(kind="composio") == "fake-key"


def test_connect_uses_server_entity_only(setup):
    client, _, fake = setup
    assert client.post(f"{BASE}/connect", json={"toolkit_slug": "metaads"}).status_code == 200
    fake.initiate_connection.assert_awaited_once_with(
        toolkit_slug="metaads", entity_id="server-owned-entity", redirect_url=None
    )


@pytest.mark.parametrize("extra", [{"entity_id": "foreign"}, {"auth_config_id": "foreign"}])
def test_connect_rejects_scope_override(setup, extra):
    client, _, fake = setup
    assert (
        client.post(f"{BASE}/connect", json={"toolkit_slug": "metaads", **extra}).status_code == 422
    )
    fake.initiate_connection.assert_not_called()


def test_delete_passes_exact_server_entity(setup):
    client, _, fake = setup
    assert client.delete(f"{BASE}/connected/ca-owned").status_code == 200
    fake.delete_connection.assert_awaited_once_with("ca-owned", entity_id="server-owned-entity")


def test_delete_foreign_connection_has_no_vendor_leak(setup):
    client, _, fake = setup
    fake.delete_connection.side_effect = ComposioApiError(404, "SECRET-FOREIGN-CONNECTION")
    response = client.delete(f"{BASE}/connected/ca-foreign")
    assert response.status_code == 404
    assert "SECRET" not in response.text


def test_rotation_preserves_server_identity_and_cannot_override_it(setup):
    client, repo, _ = setup
    assert (
        client.post(
            f"{BASE}/key", json={"api_key": "replacement", "entity_id": "foreign"}
        ).status_code
        == 422
    )
    assert client.post(f"{BASE}/key", json={"api_key": "replacement"}).status_code == 200
    assert repo.get(kind="composio").entity_id == "server-owned-entity"


@pytest.mark.usefixtures("setup")
def test_new_instances_get_distinct_server_owned_identity(tmp_path):
    identities = []
    for name in ("first", "second"):
        app = FastAPI()
        app.state.shell_webui_token = OWNER
        app.include_router(api.create_integrations_router(tmp_path / f"{name}.db"))
        client = TestClient(app, headers={"Authorization": f"Bearer {OWNER}"})
        response = client.post(f"{BASE}/key", json={"api_key": "same-project-key"})
        assert response.status_code == 200
        identities.append(response.json()["entity_id"])
    assert all(identity.startswith("safent-") for identity in identities)
    assert identities[0] != identities[1]


def test_mapping_persists_when_repository_reopens(setup):
    _, repo, _ = setup
    repo.set_auth_config(toolkit_slug="metaads", auth_config_id="ac-m")
    repo.set_auth_config(toolkit_slug="googleads", auth_config_id="ac-g")
    reopened = SQLiteIntegrationsRepository(db_path=repo._db_path, vault=SecretsVault())
    assert reopened.auth_config_ids() == {"metaads": "ac-m", "googleads": "ac-g"}


def test_unrelated_toolkits_cannot_be_administered_by_ads_route(setup):
    client, _, fake = setup
    assert (
        client.put(f"{BASE}/auth-configs/gmail", json={"auth_config_id": "ac-other"}).status_code
        == 422
    )
    fake.validate_auth_config.assert_not_called()


def test_confirmation_returns_only_scoped_metadata(setup):
    client, _, fake = setup
    response = client.get(f"{BASE}/connected/ca-owned")
    assert response.status_code == 200
    assert response.json() == {
        "id": "ca-owned",
        "toolkit_slug": "metaads",
        "entity_id": "server-owned-entity",
        "status": "ACTIVE",
        "auth_config_id": "ac-owned",
    }
    fake.get_connected_account.assert_awaited_once_with("ca-owned", entity_id="server-owned-entity")


def test_foreign_confirmation_has_no_metadata_leak(setup):
    client, _, fake = setup
    fake.get_connected_account.side_effect = ComposioApiError(404, "FOREIGN-SECRET")
    response = client.get(f"{BASE}/connected/ca-foreign")
    assert response.status_code == 404
    assert "FOREIGN-SECRET" not in response.text


def test_meta_setup_stores_only_configuration_id_and_refreshes_ads(setup):
    client, repo, fake = setup
    refresh = NS(ensure=AsyncMock())
    client.app.state.composio_lease_refresh = refresh
    result = client.post(f"{BASE}/meta/setup", json={
        "client_id": "1063816289878236", "client_secret": "NEVER-STORE-META-SECRET",
    })
    assert result.status_code == 200
    assert result.json() == {"ready": True}
    assert repo.auth_config_ids() == {"metaads": "ac-meta-setup"}
    assert b"NEVER-STORE-META-SECRET" not in repo._db_path.read_bytes()
    refresh.ensure.assert_awaited_once_with(force=True)
    fake.prepare_meta_auth_config.assert_awaited_once_with(
        client_id="1063816289878236", client_secret="NEVER-STORE-META-SECRET",
    )


@pytest.mark.parametrize("secret", ["", " ", "x" * 4097, "secret\n"])
def test_meta_setup_rejects_bad_secret_without_echo(setup, secret):
    client, _, fake = setup
    result = client.post(
        f"{BASE}/meta/setup", json={"client_id": "123456", "client_secret": secret},
    )
    assert result.status_code == 400
    fake.prepare_meta_auth_config.assert_not_called()
    assert result.json() == {"detail": "Revisa la clave de la aplicación de Meta."}


def test_meta_setup_failure_is_redacted_and_preserves_previous_mapping(setup):
    client, repo, fake = setup
    repo.set_auth_config(toolkit_slug="metaads", auth_config_id="ac-previous")
    fake.prepare_meta_auth_config.side_effect = ComposioApiError(403, "SECRET-IN-PROVIDER-ERROR")
    result = client.post(
        f"{BASE}/meta/setup", json={"client_id": "123456", "client_secret": "fake"},
    )
    assert result.status_code == 409
    assert "SECRET-IN-PROVIDER" not in result.text
    assert repo.auth_config_ids() == {"metaads": "ac-previous"}


def test_meta_setup_cannot_attach_result_to_rotated_composio_key(setup):
    client, repo, fake = setup
    async def rotate(**_kwargs):
        repo.set_credential(kind="composio", api_key="rotated-key", entity_id="server-owned-entity")
        return NS(id="ac-old-project")
    fake.prepare_meta_auth_config.side_effect = rotate
    result = client.post(
        f"{BASE}/meta/setup", json={"client_id": "123456", "client_secret": "fake"},
    )
    assert result.status_code == 409
    assert repo.auth_config_ids() == {}


@pytest.mark.parametrize("body", [
    {"client_secret": "NEVER-ECHO-SECRET"},
    {"client_id": "bad", "client_secret": "NEVER-ECHO-SECRET"},
    {"client_id": "123456", "client_secret": {"secret": "NEVER-ECHO-SECRET"}},
    {"client_id": "123456", "client_secret": "fake", "extra": "NEVER-ECHO-SECRET"},
])
def test_meta_setup_validation_never_echoes_secret_input(setup, body):
    client, _, fake = setup
    result = client.post(f"{BASE}/meta/setup", json=body)
    assert result.status_code == 400
    assert "NEVER-ECHO-SECRET" not in result.text
    fake.prepare_meta_auth_config.assert_not_called()


def test_meta_setup_limits_body_before_processing_credentials(setup):
    client, _, fake = setup
    result = client.post(f"{BASE}/meta/setup", content=b"x" * 8193)
    assert result.status_code == 413
    fake.prepare_meta_auth_config.assert_not_called()
