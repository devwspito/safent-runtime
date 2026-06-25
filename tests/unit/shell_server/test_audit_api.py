"""Tests audit/skills/consents API."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.agents_os.domain.ports.surface_adapter_port import CapturedAction, ReplayStatus
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.infrastructure.skill_store_adapter import SkillStoreAdapter
from hermes.shell_server.audit_api import create_audit_router

pytestmark = pytest.mark.unit

_FAKE_KEY = b"hermes-test-signing-key-32bytes!"


class _InMemoryKms:
    async def get_signing_key(self, *, tenant_id: object, key_id: str) -> bytes:  # noqa: ARG002
        return _FAKE_KEY


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_audit_router(tmp_path / "audit.db"))
    return TestClient(app)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Shared DB path pre-initialised by the audit router (same file)."""
    path = tmp_path / "audit.db"
    # Initialise schema the same way the shell-server does at startup.
    from hermes.shell_server.audit_api import init_schema
    init_schema(path)
    return path


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

    async def test_agent_created_skill_appears_in_list(
        self, tmp_path: Path
    ) -> None:
        """Regression: agent/chat-created skill (HITL-approved skill_manage path)
        must appear in GET /api/v1/skills after SkillStoreAdapter.replay() succeeds.

        This exercises the full daemon→DB→shell-server read path:
          skill_manage HITL approved
          → SkillStoreAdapter._upsert_skill
          → _persist_to_db (INSERT INTO skill_packages_view)
          → GET /api/v1/skills reads the same DB
          → skill visible in the Habilidades list.
        """
        db = tmp_path / "shared.db"
        skill_root = tmp_path / "skills"

        # Simulate daemon side: SkillStoreAdapter ensures schema at __init__,
        # then replay() writes the skill after HITL approval.
        adapter = SkillStoreAdapter(
            kms=_InMemoryKms(),
            db_path=db,
            skill_store_root=skill_root,
            runtime_version="test",
        )
        skill_md = (
            "---\n"
            "name: chat-created-skill\n"
            "description: Created by the agent via skill_manage\n"
            "version: '1'\n"
            "---\n\n"
            "## When\n- always\n\n"
            "## Procedure\n1. do the thing\n\n"
            "## Pitfalls\n- none\n\n"
            "## Verification\n- check the thing\n"
        )
        action = CapturedAction(
            surface_kind=SurfaceKind.SKILL_STORE,
            intent_desc="nous skill_manage create",
            payload={"action": "create", "name": "chat-created-skill", "content": skill_md},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.EXECUTED_OK, outcome.error

        # Simulate shell-server side: read from the same DB via GET /api/v1/skills.
        app = FastAPI()
        app.include_router(create_audit_router(db))
        client = TestClient(app)

        r = client.get("/api/v1/skills")
        assert r.status_code == 200
        skills = r.json()
        names = [s["skill_name"] for s in skills]
        assert "chat-created-skill" in names, (
            f"Agent-created skill missing from GET /api/v1/skills. "
            f"Got: {names}"
        )
        skill = next(s for s in skills if s["skill_name"] == "chat-created-skill")
        assert skill["state"] == "validated"
        assert skill["signing_method"] == "v2"

    async def test_skill_store_adapter_ensures_schema_on_empty_db(
        self, tmp_path: Path
    ) -> None:
        """Regression: SkillStoreAdapter.__init__ must initialise skill_packages_view
        even when the DB is new (shell-server not yet started), so a HITL-approved
        skill_manage never silently loses the row due to missing table.
        """
        db = tmp_path / "fresh.db"
        skill_root = tmp_path / "skills"

        # DB is brand new — no schema at all.
        assert not db.exists()

        adapter = SkillStoreAdapter(
            kms=_InMemoryKms(),
            db_path=db,
            skill_store_root=skill_root,
        )

        # DB must exist and skill_packages_view must be queryable after __init__.
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT COUNT(*) AS n FROM skill_packages_view").fetchone()
        conn.close()
        assert rows["n"] == 0  # empty but schema is there


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
