"""GET /api/v1/agents/roster — unit tests.

Coverage:
  - No directory stored (visibility_scope="all", the default) -> local
    roster only, BYTE-FOR-BYTE today's behaviour (regression guard).
  - Directory stored (Fase 3) -> the delivered colleague agents are
    surfaced, grouped by their own department, alongside the local roster.
  - A colleague agent_id that collides with a local one is not duplicated.
  - Daemon unavailable -> fail-soft, empty departments, never 500.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore
from hermes.shell_server.cowork.roster_api import create_roster_router
from hermes.shell_server.security.secrets import SecretsVault

pytestmark = pytest.mark.unit

_TENANT_ID = "00000000-0000-0000-0000-000000000001"
_INSTANCE_ID = "aaaaaaaa-0000-5000-8000-000000000001"


def _vault() -> SecretsVault:
    return SecretsVault(master_key=b"\xca" * 32)


def _paired_store(db_path: Path, *, directory: dict | None = None) -> SQLiteAssociationStore:
    store = SQLiteAssociationStore(db_path=db_path, vault=_vault())
    store.save(
        association=InstanceAssociation(
            instance_id=_INSTANCE_ID,
            tenant_id=_TENANT_ID,
            paired_at="2026-07-06T00:00:00+00:00",
            cloud_endpoint="https://cloud.safent.run",
            signing_pubkey_hex="deadbeef",
            license={},
            last_applied_version=0,
            state="active",
        ),
        instance_secret="sk-test",
    )
    if directory is not None:
        store.update_directory(directory)
    return store


def _make_app(*, db_path: Path, raw_agents: list[dict] | None = None) -> FastAPI:
    app = FastAPI()
    proxy = MagicMock()
    proxy.call_list = AsyncMock(return_value=raw_agents or [])
    app.state.dbus_proxy = proxy
    app.include_router(create_roster_router(db_path, _vault()))
    return app


class TestNoDirectoryRegression:
    def test_no_directory_returns_local_roster_only(self, tmp_path: Path) -> None:
        db_path = tmp_path / "shell-state.db"
        _paired_store(db_path)  # no directory pushed
        local_agents = [
            {"agent_id": "a1", "name": "Cerebro", "is_default": True},
            {"agent_id": "a2", "name": "Ventas Bot", "department": "ventas"},
        ]
        client = TestClient(_make_app(db_path=db_path, raw_agents=local_agents))

        r = client.get("/api/v1/agents/roster")

        assert r.status_code == 200
        departments = r.json()["departments"]
        all_ids = {a["id"] for d in departments for a in d["agents"]}
        assert all_ids == {"a1", "a2"}
        assert all(a["source"] != "directory" for d in departments for a in d["agents"])

    def test_unpaired_instance_returns_local_roster_only(self, tmp_path: Path) -> None:
        """No association row at all (community edition) -> no crash, no directory."""
        db_path = tmp_path / "shell-state.db"
        SQLiteAssociationStore(db_path=db_path, vault=_vault())  # creates schema, no row
        local_agents = [{"agent_id": "a1", "name": "Cerebro", "is_default": True}]
        client = TestClient(_make_app(db_path=db_path, raw_agents=local_agents))

        r = client.get("/api/v1/agents/roster")

        assert r.status_code == 200
        departments = r.json()["departments"]
        all_ids = {a["id"] for d in departments for a in d["agents"]}
        assert all_ids == {"a1"}


class TestDirectorySurfacing:
    def test_directory_colleagues_are_surfaced_grouped_by_department(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "shell-state.db"
        directory = {
            "entries": [
                {
                    "employee_id": "emp-2",
                    "agent_id": "colleague-1",
                    "name": "Ada",
                    "department": "ventas",
                },
                {
                    "employee_id": "emp-3",
                    "agent_id": "colleague-2",
                    "name": "Bob",
                    "department": "data",
                },
            ]
        }
        _paired_store(db_path, directory=directory)
        local_agents = [{"agent_id": "a1", "name": "Cerebro", "is_default": True}]
        client = TestClient(_make_app(db_path=db_path, raw_agents=local_agents))

        r = client.get("/api/v1/agents/roster")

        assert r.status_code == 200
        departments = {d["id"]: d for d in r.json()["departments"]}
        assert "colleague-1" not in {a["id"] for a in departments["cerebro"]["agents"]}
        ventas_dept = next(
            d for d in r.json()["departments"] if any(
                a["id"] == "colleague-1" for a in d["agents"]
            )
        )
        colleague = next(a for a in ventas_dept["agents"] if a["id"] == "colleague-1")
        assert colleague["name"] == "Ada"
        assert colleague["source"] == "directory"
        assert colleague["is_default"] is False

    def test_empty_directory_entries_surfaces_no_colleagues(self, tmp_path: Path) -> None:
        """visibility_scope='none' -> {"entries": []}: present but empty."""
        db_path = tmp_path / "shell-state.db"
        _paired_store(db_path, directory={"entries": []})
        local_agents = [{"agent_id": "a1", "name": "Cerebro", "is_default": True}]
        client = TestClient(_make_app(db_path=db_path, raw_agents=local_agents))

        r = client.get("/api/v1/agents/roster")

        departments = r.json()["departments"]
        all_ids = {a["id"] for d in departments for a in d["agents"]}
        assert all_ids == {"a1"}

    def test_colleague_agent_id_colliding_with_local_is_not_duplicated(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "shell-state.db"
        _paired_store(
            db_path,
            directory={
                "entries": [
                    {
                        "employee_id": "emp-2",
                        "agent_id": "a1",  # collides with the local agent below
                        "name": "Shadow",
                        "department": "ventas",
                    }
                ]
            },
        )
        local_agents = [{"agent_id": "a1", "name": "Cerebro", "is_default": True}]
        client = TestClient(_make_app(db_path=db_path, raw_agents=local_agents))

        r = client.get("/api/v1/agents/roster")

        departments = r.json()["departments"]
        matches = [a for d in departments for a in d["agents"] if a["id"] == "a1"]
        assert len(matches) == 1
        assert matches[0]["name"] == "Cerebro"  # local wins, not the directory shadow


class TestFailSoft:
    def test_daemon_unavailable_returns_empty_departments(self, tmp_path: Path) -> None:
        db_path = tmp_path / "shell-state.db"
        _paired_store(db_path)
        app = FastAPI()
        proxy = MagicMock()
        proxy.call_list = AsyncMock(side_effect=RuntimeError("daemon down"))
        app.state.dbus_proxy = proxy
        app.include_router(create_roster_router(db_path, _vault()))
        client = TestClient(app)

        r = client.get("/api/v1/agents/roster")

        assert r.status_code == 200
        assert r.json() == {"departments": []}

    def test_directory_read_failure_falls_back_to_local_roster_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken association-store read must never break the roster endpoint."""
        from hermes.instance import association_store as association_store_mod

        db_path = tmp_path / "shell-state.db"
        _paired_store(db_path, directory={"entries": [
            {"employee_id": "e", "agent_id": "colleague-1", "name": "Ada", "department": "ventas"},
        ]})

        def _boom(*, db_path: Path, vault: SecretsVault) -> SQLiteAssociationStore:
            raise RuntimeError("db locked")

        monkeypatch.setattr(association_store_mod, "SQLiteAssociationStore", _boom)
        local_agents = [{"agent_id": "a1", "name": "Cerebro", "is_default": True}]
        client = TestClient(_make_app(db_path=db_path, raw_agents=local_agents))

        r = client.get("/api/v1/agents/roster")

        assert r.status_code == 200
        all_ids = {a["id"] for d in r.json()["departments"] for a in d["agents"]}
        assert all_ids == {"a1"}
