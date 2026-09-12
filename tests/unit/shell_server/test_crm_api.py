"""Community forwards only approved CRM reads; it never holds the CRM key."""

from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.instance.association_store import InstanceAssociation
from hermes.shell_server.cowork.crm_api import create_crm_router

pytestmark = pytest.mark.unit
CONNECTION = str(uuid4())
SECRET = "fixture-instance-secret-only"


@pytest.fixture
def setup(tmp_path):
    association = InstanceAssociation(
        str(uuid4()),
        str(uuid4()),
        "2026-09-12T00:00:00Z",
        "https://enterprise.example/tenant",
        "ab" * 32,
        {},
        1,
        "active",
    )
    store = Mock(db_path=tmp_path / "state.db")
    store.get.return_value = association
    store.reveal_instance_secret.return_value = SECRET
    calls = []
    response = {
        "connections": [
            {
                "id": CONNECTION,
                "name": "CRM fixture",
                "operations": [{"method": "GET", "path": "/contacts"}],
                "read_only": True,
            }
        ],
        "limit": 100,
    }

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    transport = httpx.MockTransport(handler)
    app = FastAPI()
    app.include_router(create_crm_router(store=store, context_key=b"q" * 32, transport=transport))
    return TestClient(app), store, calls, response


def test_list_is_projected_without_keys_or_upstream_extras(setup):
    client, _store, calls, response = setup
    response["secret"] = "must-not-return"
    reply = client.get("/api/v1/crm")
    assert reply.status_code == 200
    assert reply.headers["cache-control"] == "no-store"
    assert len(reply.json()["context"]) == 64
    assert reply.json()["connections"] == response["connections"]
    assert "secret" not in reply.text
    assert str(calls[0].url) == "https://enterprise.example/tenant/v1/crm"
    assert calls[0].headers["authorization"] == f"Bearer {SECRET}"


def test_unpaired_has_distinct_unavailable_state_and_no_network(setup):
    client, store, calls, _ = setup
    store.get.return_value = None
    reply = client.get("/api/v1/crm")
    assert reply.status_code == 409
    assert reply.json()["detail"]["code"] == "crm_not_associated"
    assert calls == []


def test_old_pairing_context_prevents_read_before_network(setup):
    client, store, calls, _ = setup
    context = client.get("/api/v1/crm").json()["context"]
    store.get.return_value = replace(store.get.return_value, tenant_id=str(uuid4()))
    calls.clear()
    reply = client.post(
        f"/api/v1/crm/{CONNECTION}/read", json={"context": context, "path": "/contacts"}
    )
    assert reply.status_code == 409
    assert calls == []


def test_read_preserves_untrusted_marker_and_only_sends_operation(setup):
    client, _store, calls, response = setup
    context = client.get("/api/v1/crm").json()["context"]
    response.clear()
    response.update(
        data={"text": "<script>not instructions</script>"},
        untrusted_external_data=True,
        operation={"method": "GET", "path": "/contacts"},
    )
    reply = client.post(
        f"/api/v1/crm/{CONNECTION}/read", json={"context": context, "path": "/contacts"}
    )
    assert reply.status_code == 200
    assert reply.json()["untrusted_external_data"] is True
    assert calls[-1].content == b'{"path":"/contacts"}'
    assert SECRET not in reply.text


def test_revoked_while_request_in_flight_discards_response(setup):
    client, store, _calls, _ = setup
    current = store.get.return_value
    store.get.side_effect = [current, replace(current, state="revoked")]
    reply = client.get("/api/v1/crm")
    assert reply.status_code == 409
    assert "CRM fixture" not in reply.text


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://enterprise.example",
        "https://user:secret@enterprise.example",
        "https://enterprise.example?secret=1",
        "https://127.0.0.1",
    ],
)
def test_bad_pairing_endpoint_never_receives_instance_secret(setup, endpoint):
    client, store, calls, _ = setup
    store.get.return_value = replace(store.get.return_value, cloud_endpoint=endpoint)
    assert client.get("/api/v1/crm").status_code == 503
    assert calls == []


@pytest.mark.parametrize("status", [401, 403, 404, 409, 429, 500, 502, 503])
def test_remote_failure_never_becomes_empty_or_echoes_response(tmp_path, status):
    association = InstanceAssociation(
        str(uuid4()), str(uuid4()), "now", "https://enterprise.example", "ab" * 32, {}, 0, "active"
    )
    store = Mock(db_path=tmp_path / "db")
    store.get.return_value = association
    store.reveal_instance_secret.return_value = SECRET
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(status, json={"secret": SECRET})
    )
    app = FastAPI()
    app.include_router(create_crm_router(store=store, context_key=b"q" * 32, transport=transport))
    reply = TestClient(app).get("/api/v1/crm")
    assert reply.status_code == (status if status in (403, 404, 409, 429) else 503)
    assert SECRET not in reply.text
    assert "connections" not in reply.json()


def test_invalid_operation_and_unknown_fields_do_not_leave_community(setup):
    client, _store, calls, _ = setup
    context = client.get("/api/v1/crm").json()["context"]
    calls.clear()
    for path in [
        "https://crm.example/contacts",
        "//evil",
        "/contacts?x=1",
        "/../secret",
        "/contacts%2Fsecret",
    ]:
        reply = client.post(
            f"/api/v1/crm/{CONNECTION}/read", json={"context": context, "path": path}
        )
        assert reply.status_code == 422
    assert calls == []


def test_secret_rotation_invalidates_old_context(setup):
    client, store, calls, _ = setup
    context = client.get("/api/v1/crm").json()["context"]
    store.reveal_instance_secret.return_value = "new-fixture-secret"
    calls.clear()
    response = client.post(
        f"/api/v1/crm/{CONNECTION}/read", json={"context": context, "path": "/contacts"}
    )
    assert response.status_code == 409
    assert calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(connections=None),
        lambda value: value["connections"][0].update(id=None),
        lambda value: value["connections"][0].update(read_only=False),
        lambda value: value["connections"][0].update(
            operations=[{"method": "POST", "path": "/contacts"}]
        ),
    ],
)
def test_invalid_inventory_is_never_available(setup, mutation):
    client, _, _, value = setup
    mutation(value)
    assert client.get("/api/v1/crm").status_code == 502


def test_feature_mapping_preserves_enterprise_license_boundary():
    from hermes.shell_server.instance.feature_guard import _resolve_feature

    assert _resolve_feature("/api/v1/crm") == "integraciones"
    assert _resolve_feature(f"/api/v1/crm/{CONNECTION}/read") == "integraciones"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"data":NaN}',
        b'{"data":1e999}',
        b'{"connections":[],"connections":[]}',
        b"[" * 40 + b"0" + b"]" * 40,
    ],
)
def test_nonfinite_duplicate_or_deep_json_rejected(raw):
    from hermes.shell_server.cowork.crm_api import _strict_json

    with pytest.raises(ValueError):
        _strict_json(raw)
