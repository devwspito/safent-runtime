"""Tests training API endpoints."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.agents_os.application.teaching.input_ownership_ledger import InputOwnershipLedger
from hermes.agents_os.application.teaching.teaching_session_orchestrator import (
    TeachingSessionOrchestrator,
)
from hermes.agents_os.testing.fake_teaching_context import FakeTeachingContext
from hermes.shell_server.training.api import _get_orchestrator, create_training_router

pytestmark = pytest.mark.unit


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_training_router(tmp_path / "training.db"))
    return TestClient(app)


@pytest.fixture
def teaching_client(tmp_path: Path) -> tuple[TestClient, Path]:
    """Client wired exactly like main.py: teaching_orch shares the same
    TrainingSessionOrchestrator singleton that the router uses internally."""
    db_path = tmp_path / "training.db"
    training_orch = _get_orchestrator(db_path)
    teaching_orch = TeachingSessionOrchestrator(
        training_orchestrator=training_orch,
        context_factory=FakeTeachingContext(),
        ledger=InputOwnershipLedger(),
    )
    app = FastAPI()
    app.include_router(
        create_training_router(db_path, teaching_orchestrator=teaching_orch)
    )
    return TestClient(app), db_path


class TestLifecycle:
    def test_full_happy_path(self, client: TestClient) -> None:
        # Create.
        r = client.post(
            "/api/v1/training",
            json={"skill_name": "subir-iva-303", "description": "AEAT 303"},
        )
        assert r.status_code == 200
        sid = r.json()["session_id"]
        assert r.json()["state"] == "idle"

        # Start.
        r = client.post(f"/api/v1/training/{sid}/start")
        assert r.json()["state"] == "capturing"

        # Stop.
        r = client.post(f"/api/v1/training/{sid}/stop")
        assert r.json()["state"] == "review"

        # Sign: spec 004/US3 — sign produces 'validated', not 'signed'.
        r = client.post(f"/api/v1/training/{sid}/sign")
        assert r.json()["state"] == "validated"

    def test_start_twice_blocked(self, client: TestClient) -> None:
        r = client.post("/api/v1/training", json={"skill_name": "x"})
        sid = r.json()["session_id"]
        client.post(f"/api/v1/training/{sid}/start")
        r2 = client.post(f"/api/v1/training/{sid}/start")
        assert r2.status_code == 409

    def test_sign_without_review_blocked(self, client: TestClient) -> None:
        r = client.post("/api/v1/training", json={"skill_name": "x"})
        sid = r.json()["session_id"]
        r2 = client.post(f"/api/v1/training/{sid}/sign")
        assert r2.status_code == 409

    def test_abandon_from_any_state(self, client: TestClient) -> None:
        r = client.post("/api/v1/training", json={"skill_name": "x"})
        sid = r.json()["session_id"]
        client.post(f"/api/v1/training/{sid}/start")
        r2 = client.post(f"/api/v1/training/{sid}/abandon")
        assert r2.json()["state"] == "abandoned"


class TestList:
    def test_list_empty(self, client: TestClient) -> None:
        assert client.get("/api/v1/training").json() == []

    def test_list_after_create(self, client: TestClient) -> None:
        client.post("/api/v1/training", json={"skill_name": "skill-a"})
        client.post("/api/v1/training", json={"skill_name": "skill-b"})
        items = client.get("/api/v1/training").json()
        assert len(items) == 2


class TestTeachingDoubleStart:
    """Regression: FR-003 — open_teaching_session must NOT pre-start the
    TrainingSessionOrchestrator.  If it does, the subsequent POST /start
    silently overwrites the in-memory session, orphaning the context-linked
    session (the double-start bug).

    Guard: after create → start, the orchestrator must contain exactly ONE
    session keyed by the DB UUID, created by /start — not two, not zero.
    """

    def test_no_session_in_orchestrator_before_start(
        self, teaching_client: tuple[TestClient, Path]
    ) -> None:
        """POST /training (create) must NOT create a TrainingSession in the
        orchestrator.  Only POST /training/{id}/start may do that."""
        client, db_path = teaching_client
        orch = _get_orchestrator(db_path)

        r = client.post(
            "/api/v1/training",
            json={"skill_name": "pay-invoice", "site_id": "site-a"},
        )
        assert r.status_code == 200
        sid = UUID(r.json()["session_id"])

        # The orchestrator must NOT know about this session yet.
        with pytest.raises(Exception):
            orch.get_session(session_id=sid)

    def test_exactly_one_session_after_start(
        self, teaching_client: tuple[TestClient, Path]
    ) -> None:
        """POST /start must result in exactly one TrainingSession in the
        orchestrator — not two (open+start) and not zero."""
        client, db_path = teaching_client
        orch = _get_orchestrator(db_path)

        r = client.post(
            "/api/v1/training",
            json={"skill_name": "pay-invoice", "site_id": "site-a"},
        )
        sid = UUID(r.json()["session_id"])

        r2 = client.post(f"/api/v1/training/{sid}/start")
        assert r2.status_code == 200

        # Exactly one session, keyed by the DB UUID.
        sess = orch.get_session(session_id=sid)
        assert sess.session_id == sid

        # Verify there are no phantom sessions (dict size == 1).
        assert len(orch._sessions) == 1
