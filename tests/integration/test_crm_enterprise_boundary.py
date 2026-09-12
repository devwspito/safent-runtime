"""Opt-in cross-repository CRM contract, real SQLite/service/HTTP, fake CRM only.

Set PYTHONPATH=src:<enterprise>/src:<enterprise>. No deployment or live secret.
"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore
from hermes.shell_server.cowork.crm_api import create_crm_router
from hermes.shell_server.security.secrets import SecretsVault

pytestmark = pytest.mark.integration


def _revision(row, **changes):
    return {
        "operation_id": uuid4(),
        "revision": row["revision"],
        "plan_hash": row["plan_hash"],
        "totp": "123456",
        **changes,
    }


def _prepare(repo):
    from safent_control.application.crm import CrmService
    from safent_control.domain.crm import Activate, Actor, Credential, Draft, Principal, Probe
    from safent_control.domain.entities import (
        AgentTemplate,
        ConsoleSession,
        Employee,
        Instance,
        InstanceState,
        Membership,
        MembershipRole,
        Tenant,
        User,
    )
    from safent_control.infrastructure.repository import hash_secret

    class Transport:
        def __init__(self):
            self.calls = []

        async def get(self, **kwargs):
            self.calls.append(kwargs)
            return {"data": {"contacts": [{"name": "Fictitious"}]}, "size_bytes": 44}

    org, user, session, employee, template, instance_id = [str(uuid4()) for _ in range(6)]
    repo.save_tenant(Tenant(org, "CRM fixture", 10))
    repo.save_user(User(user, "owner@fixture.test", "Fixture"))
    repo.save_membership(Membership(str(uuid4()), user, org, MembershipRole.OWNER))
    repo.save_session(ConsoleSession(session, user, org, datetime.now(UTC) + timedelta(hours=1)))
    repo.save_employee(
        Employee(employee_id=employee, org_id=org, email="worker@fixture.test", name="Worker")
    )
    repo.save_agent_template(
        AgentTemplate(agent_template_id=template, employee_id=employee, org_id=org, name="Fixture")
    )
    instance = Instance(
        instance_id=instance_id,
        org_id=org,
        agent_template_id=template,
        hardware_fingerprint="fixture",
        state=InstanceState.ACTIVE,
    )
    secret = "fixture-instance-credential-not-the-crm-key"
    repo.save_instance(instance, instance_secret_hash=hash_secret(secret))
    owner = Actor(org, "human", user, session)
    transport = Transport()
    service = CrmService(repo, transport=transport, step_up=lambda _user, _code: True)
    row = service.create(
        owner,
        Draft(
            operation_id=uuid4(),
            name="CRM fixture",
            document_json=json.dumps(
                {
                    "openapi": "3.1.0",
                    "servers": [{"url": "https://crm.example.com/api"}],
                    "paths": {"/contacts": {"get": {"operationId": "contacts"}}},
                }
            ),
            server="https://crm.example.com/api",
            paths=["/contacts"],
        ),
    )
    row = service.mutate(
        owner,
        row["id"],
        "credential",
        Credential(**_revision(row, secret="fictitious-private-token")),
    )
    row = asyncio.run(
        service.read(
            owner,
            row["id"],
            "/contacts",
            Probe(**_revision(row, path="/contacts", confirm_read_only=True)),
        )
    )
    row = service.mutate(
        owner,
        row["id"],
        "activate",
        Activate(**_revision(row, grants=[Principal(kind="instance", id=instance_id)])),
    )
    return service, owner, row, instance, secret, transport


def test_real_enterprise_grant_read_revocation_and_repair(tmp_path, monkeypatch):  # noqa: PLR0915 - cross-repo boundary scenario
    pytest.importorskip("safent_control")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("SESSION_SECRET", "fixture-crm-session-signing-material-only")
    monkeypatch.setenv("SAFENT_DATA_DIR", str(tmp_path / "enterprise-data"))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    from safent_control.api import crm, deps
    from safent_control.domain.crm import Reviewed
    from safent_control.infrastructure.config import get_settings
    from safent_control.infrastructure.repository import ControlPlaneRepository

    get_settings.cache_clear()
    repo = ControlPlaneRepository(db_path=tmp_path / "enterprise.sqlite")
    service, owner, row, instance, secret, transport = _prepare(repo)
    monkeypatch.setattr(deps, "get_repo", lambda: repo)
    monkeypatch.setattr(crm, "service", lambda: service)
    enterprise = FastAPI()
    enterprise.include_router(crm.instance_router)
    enterprise.add_exception_handler(crm.CrmError, crm.crm_error_handler)

    vault = SecretsVault(master_key=b"c" * 32)
    store = SQLiteAssociationStore(db_path=tmp_path / "community.sqlite", vault=vault)
    association = InstanceAssociation(
        instance.instance_id,
        instance.org_id,
        "2026-09-12T00:00:00Z",
        "https://enterprise.example",
        "ab" * 32,
        {},
        0,
        "active",
    )
    store.save(association=association, instance_secret=secret)
    app = FastAPI()
    app.include_router(
        create_crm_router(
            store=store, context_key=b"q" * 32, transport=httpx.ASGITransport(app=enterprise)
        )
    )
    client = TestClient(app)
    inventory = client.get("/api/v1/crm")
    assert inventory.status_code == 200, inventory.text
    assert inventory.json()["connections"][0]["id"] == row["id"]
    intent = {"context": inventory.json()["context"], "path": "/contacts"}
    read = client.post(f"/api/v1/crm/{row['id']}/read", json=intent)
    assert read.status_code == 200, read.text
    assert read.json()["data"]["contacts"][0]["name"] == "Fictitious"
    assert read.json()["untrusted_external_data"] is True
    assert "fictitious-private-token" not in read.text
    count = len(transport.calls)

    denied = client.post(f"/api/v1/crm/{row['id']}/read", json={**intent, "path": "/unapproved"})
    assert denied.status_code == 403
    assert len(transport.calls) == count

    service.mutate(owner, row["id"], "revoke", Reviewed(**_revision(row)))
    revoked = client.post(f"/api/v1/crm/{row['id']}/read", json=intent)
    assert revoked.status_code in (403, 404)
    assert len(transport.calls) == count
    assert client.get("/api/v1/crm").json()["connections"] == []

    store.save(
        association=replace(association, paired_at="2026-09-12T01:00:00Z"), instance_secret=secret
    )
    assert client.post(f"/api/v1/crm/{row['id']}/read", json=intent).status_code == 409
    assert len(transport.calls) == count
    get_settings.cache_clear()
