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
    def test_list_empty_on_fresh_db(self, client: TestClient) -> None:
        """The audit endpoint is a read-only projection of the native signed
        chain (audit_entries_view). A fresh DB carries NO fabricated rows: the
        old demo seed injected entries with an INVENTED signature ("abcd1234…")
        on every boot, indistinguishable from real signed entries and outside
        the verifiable chain. It was removed as false governance (commit
        0bca2c0). The endpoint must therefore return an empty list, never
        fabricated audit entries.
        """
        r = client.get("/api/v1/audit")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_projects_native_rows_newest_first(
        self, client: TestClient, db_path: Path
    ) -> None:
        """The endpoint projects rows the native AuditHashChainSigner writes into
        audit_entries_view — verbatim and ordered newest-first — and invents
        nothing of its own.
        """
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        conn.executemany(
            "INSERT INTO audit_entries_view "
            "(entry_id, timestamp, actor, audit_kind, category, description, "
            "signature_short) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "e1",
                    "2026-01-01T00:00:00Z",
                    "owner",
                    "node_install_created",
                    "install",
                    "node install approved",
                    "deadbeef",
                ),
                (
                    "e2",
                    "2026-01-02T00:00:00Z",
                    "owner",
                    "tenant_bound",
                    "tenant",
                    "tenant bound to node",
                    "cafebabe",
                ),
            ],
        )
        conn.commit()
        conn.close()

        r = client.get("/api/v1/audit")
        assert r.status_code == 200
        entries = r.json()
        # Ordered by timestamp DESC → the newer 'tenant_bound' row comes first.
        assert [e["audit_kind"] for e in entries] == [
            "tenant_bound",
            "node_install_created",
        ]
        # Projection is verbatim: the signature is read from the native chain,
        # not re-synthesised by this read-only layer.
        assert entries[0]["signature_short"] == "cafebabe"
        assert entries[0]["actor"] == "owner"


class TestSkills:
    def test_list_empty(self, client: TestClient) -> None:
        # Sin seed para skills.
        r = client.get("/api/v1/skills")
        assert r.status_code == 200
        assert r.json() == []

    async def test_agent_created_skill_appears_in_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: agent/chat-created skill (HITL-approved skill_manage path)
        must appear in GET /api/v1/skills after SkillStoreAdapter.replay() succeeds.

        New architecture: skills go to $HERMES_HOME/skills/<name>/SKILL.md (no DB row).
        The endpoint reads directly from disk when D-Bus is unavailable (CI path).
        """
        hermes_home = tmp_path / "hermes-home"
        skill_root = hermes_home / "skills"
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        db = tmp_path / "shared.db"
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

        # Shell-server side: no dbus_proxy, fallback reads $HERMES_HOME/skills/.
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
        assert skill["signing_method"] == "v2"
        # state is 'validated' when native keystore is available, or kept as written
        # when key is absent (CI). Either way the skill must be listed.
        assert skill["state"] in ("validated", "unverified")

    async def test_skill_store_adapter_ensures_governance_schema_on_empty_db(
        self, tmp_path: Path
    ) -> None:
        """Regression: SkillStoreAdapter.__init__ must create the governance schema
        (skill_packages_view via SkillGovernanceService) even on a fresh DB, so
        promote/deprecate operations never fail with 'no such table'.

        Note: SkillStoreAdapter no longer writes skill rows to skill_packages_view
        (that table is used exclusively for governance state mutations now).
        """
        db = tmp_path / "fresh.db"
        skill_root = tmp_path / "skills"

        # DB is brand new — no schema at all.
        assert not db.exists()

        SkillStoreAdapter(
            kms=_InMemoryKms(),
            db_path=db,
            skill_store_root=skill_root,
        )

        # skill_packages_view must exist (governance schema) even though it's empty.
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT COUNT(*) AS n FROM skill_packages_view").fetchone()
        conn.close()
        assert rows["n"] == 0  # empty but governance schema is present


# NOTE: The consents REST surface (GET/POST/DELETE /api/v1/consents + its DTOs
# and the parallel `consents_view` table) was DELIBERATELY REMOVED in commit
# 0bca2c0 ("eliminar ... store de consent paralelo"). It was a parallel consent
# store that bypassed the native grant_consent path and the signed audit chain
# → it diverged from the real gate ("falsa gobernanza"). The single source of
# truth is now the native ConsentManager over D-Bus (grant_consent /
# revoke_consent / list_consents → consent_grants), which the daemon gate and
# the QML security app consume directly. audit_api serves NO consents endpoints
# (verified absent in source and image), so the former TestConsents suite — which
# asserted behaviour of those removed endpoints — was dead and has been deleted.
# The capability-validation invariant it once guarded (reject unknown/empty/
# hallucinated capabilities) now lives with the native ConsentManager, not this
# read-only projection layer.
