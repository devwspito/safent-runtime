"""Tests audit/skills/consents API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.audit_api import create_audit_router

pytestmark = pytest.mark.unit


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_audit_router(tmp_path / "audit.db"))
    return TestClient(app)


class TestAudit:
    def test_list_seeded(self, client: TestClient) -> None:
        r = client.get("/api/v1/audit")
        assert r.status_code == 200
        entries = r.json()
        assert len(entries) >= 4
        kinds = {e["audit_kind"] for e in entries}
        assert "node_install_created" in kinds
        assert "tenant_bound" in kinds


class TestSkills:
    def test_list_empty(self, client: TestClient) -> None:
        # Sin seed para skills.
        r = client.get("/api/v1/skills")
        assert r.status_code == 200
        assert r.json() == []


class TestConsents:
    def test_grant_and_list(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/consents",
            json={"capability": "documents", "scope": "session"},
        )
        assert r.status_code == 201
        cid = r.json()["consent_id"]
        items = client.get("/api/v1/consents").json()
        assert any(c["consent_id"] == cid for c in items)

    def test_revoke(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/consents",
            json={"capability": "terminal", "scope": "once"},
        )
        cid = r.json()["consent_id"]
        client.delete(f"/api/v1/consents/{cid}")
        # Por defecto excluye revoked.
        active = client.get("/api/v1/consents").json()
        assert not any(c["consent_id"] == cid for c in active)
        # Con include_revoked sí aparece.
        all_ = client.get(
            "/api/v1/consents?include_revoked=true"
        ).json()
        revoked = [c for c in all_ if c["consent_id"] == cid]
        assert len(revoked) == 1
        assert revoked[0]["revoked_at"] is not None

    # --- Regression tests for finding #23 ---

    def test_grant_empty_capability_is_rejected(self, client: TestClient) -> None:
        """Regression #23: empty-string capability must be rejected with 422."""
        r = client.post("/api/v1/consents", json={"capability": ""})
        assert r.status_code == 422

    def test_grant_unknown_capability_is_rejected(self, client: TestClient) -> None:
        """Regression #23: arbitrary/unknown capability strings must be rejected with 422."""
        r = client.post(
            "/api/v1/consents",
            json={"capability": "contacts", "scope": "session"},
        )
        assert r.status_code == 422

    def test_grant_hallucinated_capability_is_rejected(self, client: TestClient) -> None:
        """Regression #23: LLM-hallucinated capability names must be rejected with 422."""
        r = client.post(
            "/api/v1/consents",
            json={"capability": "filesystem", "scope": "session"},
        )
        assert r.status_code == 422

    def test_grant_valid_capability_persists_enum_value(self, client: TestClient) -> None:
        """Regression #23: known capability is stored and returned as its canonical string value."""
        r = client.post(
            "/api/v1/consents",
            json={"capability": "microphone", "scope": "persistent"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["capability"] == "microphone"
        assert body["scope"] == "persistent"

    def test_grant_defaults_scope_to_session(self, client: TestClient) -> None:
        """Regression #23: omitting scope defaults to 'session', not an empty string."""
        r = client.post(
            "/api/v1/consents",
            json={"capability": "downloads"},
        )
        assert r.status_code == 201
        assert r.json()["scope"] == "session"
