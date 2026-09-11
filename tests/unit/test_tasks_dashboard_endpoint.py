"""Tests — GET /api/v1/tasks/dashboard (docs/logica-pendiente-2026-09-11 §1 /
SAFENT-PENDIENTES.md §5).

Covers:
  1. SqliteTasksDashboardRepository — source derivation, approval_ids
     desconocido/vacío, has_more (limit+1), ordering, result enrichment,
     fail-closed on missing primary source.
  2. Shell-server route — 200 available + shape, 503 (never a false []) on
     source failure, 401 without bearer via the real app.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.capabilities.infrastructure.schema import ensure_capabilities_schema
from hermes.tasks.infrastructure.schema import ensure_tasks_schema
from hermes.tasks.infrastructure.sqlite_conversation_repo import (
    SQLiteConversationRepository,
)
from hermes.tasks.infrastructure.sqlite_pending_delegations import (
    _DDL_PENDING_DELEGATIONS,
)
from hermes.tasks.control_plane.infrastructure.sqlite_tasks_dashboard_repository import (
    SqliteTasksDashboardRepository,
    TasksDashboardUnavailable,
)

pytestmark = pytest.mark.unit


# ===========================================================================
# Helpers
# ===========================================================================


def _full_db(tmp_path: Path) -> Path:
    """A shell-state.db with EVERY table the dashboard reads (production
    shape once the daemon has run at least once)."""
    db_path = tmp_path / "shell-state.db"
    conn = sqlite3.connect(str(db_path))
    ensure_tasks_schema(conn)
    ensure_capabilities_schema(conn)
    conn.executescript(_DDL_PENDING_DELEGATIONS)
    conn.commit()
    conn.close()
    SQLiteConversationRepository(db_path=db_path)  # applies messages schema
    return db_path


def _insert_task(
    db_path: Path,
    *,
    status: str = "pending",
    created_at: str | None = None,
    conversation_id: str | None = None,
    instruction: str = "do the thing",
) -> str:
    task_id = str(uuid4())
    now = created_at or datetime.now(tz=UTC).isoformat()
    owner = str(uuid4())
    payload = json.dumps({"enqueued_by": owner, "instruction": instruction})
    execution_fields = (
        (str(uuid4()), "a" * 64) if status == "completed" else (None, None)
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO agent_tasks (
            task_id, trigger_kind, enqueued_by, operator_id,
            instruction, payload_json, status,
            kind, priority, retry_count, max_retries,
            execution_audit_entry_id, execution_head_hash,
            created_at, updated_at
        ) VALUES (?, 'manual_enqueue', ?, ?, ?, ?, ?, 'autonomous', 0, 0, 3, ?, ?, ?, ?)
        """,
        (
            task_id,
            owner,
            owner,
            instruction,
            payload,
            status,
            *execution_fields,
            now,
            now,
        ),
    )
    if conversation_id is not None:
        conn.execute(
            "UPDATE agent_tasks SET conversation_id = ? WHERE task_id = ?",
            (conversation_id, task_id),
        )
    conn.commit()
    conn.close()
    return task_id


def _link_delegation(db_path: Path, *, task_id: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO pending_delegations (
            message_id, correlation_id, from_employee_id, from_instance_id,
            body, issued_at, status, task_id, created_at
        ) VALUES (?, ?, 'peer-employee', 'peer-instance', 'hola', ?, 'approved', ?, ?)
        """,
        (str(uuid4()), str(uuid4()), datetime.now(tz=UTC).isoformat(), task_id,
         datetime.now(tz=UTC).isoformat()),
    )
    conn.commit()
    conn.close()


def _insert_approval(db_path: Path, *, task_id: str) -> str:
    proposal_id = str(uuid4())
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO pending_approvals (
            proposal_id, work_item_id, operator_id, risk, created_at
        ) VALUES (?, ?, ?, 'high', ?)
        """,
        (proposal_id, task_id, str(uuid4()), datetime.now(tz=UTC).isoformat()),
    )
    conn.commit()
    conn.close()
    return proposal_id


def _insert_assistant_message(db_path: Path, *, task_id: str, content: str) -> None:
    conv_id = str(uuid4())
    now = datetime.now(tz=UTC).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO conversations (conversation_id, title, started_at, last_msg_at) "
        "VALUES (?, 't', ?, ?)",
        (conv_id, now, now),
    )
    conn.execute(
        "INSERT INTO messages (message_id, conversation_id, role, content, created_at, task_id) "
        "VALUES (?, ?, 'assistant', ?, ?, ?)",
        (str(uuid4()), conv_id, content, now, task_id),
    )
    conn.commit()
    conn.close()


# ===========================================================================
# 1. SqliteTasksDashboardRepository
# ===========================================================================


class TestSqliteTasksDashboardRepository:
    def test_task_without_delegation_link_is_local(self, tmp_path: Path) -> None:
        db_path = _full_db(tmp_path)
        _insert_task(db_path)

        repo = SqliteTasksDashboardRepository(db_path=db_path)
        views, has_more = repo.list_dashboard_tasks(limit=10)

        assert len(views) == 1
        assert views[0].source == "local"
        assert has_more is False

    def test_task_with_delegation_link_is_enterprise(self, tmp_path: Path) -> None:
        db_path = _full_db(tmp_path)
        task_id = _insert_task(db_path)
        _link_delegation(db_path, task_id=task_id)

        repo = SqliteTasksDashboardRepository(db_path=db_path)
        views, _ = repo.list_dashboard_tasks(limit=10)

        assert views[0].source == "enterprise"

    def test_approval_ids_empty_when_checked_and_none_found(
        self, tmp_path: Path
    ) -> None:
        db_path = _full_db(tmp_path)
        _insert_task(db_path)

        repo = SqliteTasksDashboardRepository(db_path=db_path)
        views, _ = repo.list_dashboard_tasks(limit=10)

        assert views[0].approval_ids == ()

    def test_approval_ids_populated_when_links_exist(self, tmp_path: Path) -> None:
        db_path = _full_db(tmp_path)
        task_id = _insert_task(db_path)
        proposal_id = _insert_approval(db_path, task_id=task_id)

        repo = SqliteTasksDashboardRepository(db_path=db_path)
        views, _ = repo.list_dashboard_tasks(limit=10)

        assert views[0].approval_ids == (proposal_id,)

    def test_approval_ids_unknown_when_pending_approvals_table_missing(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "shell-state.db"
        conn = sqlite3.connect(str(db_path))
        ensure_tasks_schema(conn)
        conn.executescript(_DDL_PENDING_DELEGATIONS)
        conn.commit()
        conn.close()
        SQLiteConversationRepository(db_path=db_path)
        _insert_task(db_path)  # pending_approvals NEVER created (daemon-only table)

        repo = SqliteTasksDashboardRepository(db_path=db_path)
        views, _ = repo.list_dashboard_tasks(limit=10)

        assert views[0].approval_ids is None

    def test_has_more_true_when_more_rows_than_limit(self, tmp_path: Path) -> None:
        db_path = _full_db(tmp_path)
        for i in range(3):
            _insert_task(db_path, created_at=f"2026-05-0{i + 1}T08:00:00+00:00")

        repo = SqliteTasksDashboardRepository(db_path=db_path)
        views, has_more = repo.list_dashboard_tasks(limit=2)

        assert len(views) == 2
        assert has_more is True

    def test_has_more_false_when_rows_exactly_fill_limit(
        self, tmp_path: Path
    ) -> None:
        db_path = _full_db(tmp_path)
        _insert_task(db_path)

        repo = SqliteTasksDashboardRepository(db_path=db_path)
        views, has_more = repo.list_dashboard_tasks(limit=1)

        assert has_more is False

    def test_ordering_newest_first(self, tmp_path: Path) -> None:
        db_path = _full_db(tmp_path)
        old_id = _insert_task(db_path, created_at="2026-05-01T08:00:00+00:00")
        new_id = _insert_task(db_path, created_at="2026-05-02T08:00:00+00:00")

        repo = SqliteTasksDashboardRepository(db_path=db_path)
        views, _ = repo.list_dashboard_tasks(limit=10)

        assert [v.task_id for v in views] == [new_id, old_id]

    def test_result_populated_for_completed_task(self, tmp_path: Path) -> None:
        db_path = _full_db(tmp_path)
        task_id = _insert_task(db_path, status="completed")
        _insert_assistant_message(db_path, task_id=task_id, content="done: 42")

        repo = SqliteTasksDashboardRepository(db_path=db_path)
        views, _ = repo.list_dashboard_tasks(limit=10)

        assert views[0].result == "done: 42"

    def test_result_absent_for_pending_task(self, tmp_path: Path) -> None:
        db_path = _full_db(tmp_path)
        _insert_task(db_path, status="pending")

        repo = SqliteTasksDashboardRepository(db_path=db_path)
        views, _ = repo.list_dashboard_tasks(limit=10)

        assert views[0].result is None

    def test_conversation_id_surfaced_when_present(self, tmp_path: Path) -> None:
        db_path = _full_db(tmp_path)
        conv_id = str(uuid4())
        _insert_task(db_path, conversation_id=conv_id)

        repo = SqliteTasksDashboardRepository(db_path=db_path)
        views, _ = repo.list_dashboard_tasks(limit=10)

        assert views[0].conversation_id == conv_id

    def test_raises_unavailable_when_agent_tasks_table_missing(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "empty.db"
        sqlite3.connect(str(db_path)).close()  # file exists, NO schema applied

        repo = SqliteTasksDashboardRepository(db_path=db_path)

        with pytest.raises(TasksDashboardUnavailable):
            repo.list_dashboard_tasks(limit=10)


# ===========================================================================
# 2. Shell-server route — minimal app (mirrors test_tasks_dashboard.py §7)
# ===========================================================================


class _FakeDashboardRepoAvailable:
    def list_dashboard_tasks(self, *, limit: int) -> tuple[list[Any], bool]:
        from hermes.tasks.control_plane.domain.ports import DashboardTaskView

        return (
            [
                DashboardTaskView(
                    task_id="t1",
                    label="do the thing",
                    status="completed",
                    source="enterprise",
                    approval_ids=("p1",),
                )
            ],
            False,
        )


class _FakeDashboardRepoUnavailable:
    def list_dashboard_tasks(self, *, limit: int) -> tuple[list[Any], bool]:
        raise TasksDashboardUnavailable("db locked")


def _make_dashboard_app(repo: Any) -> FastAPI:
    from fastapi import HTTPException  # noqa: PLC0415

    from hermes.shell_server.main import _dashboard_task_to_dict  # noqa: PLC0415

    app = FastAPI()

    @app.get("/api/v1/tasks/dashboard")
    async def tasks_dashboard(limit: int = 100) -> dict[str, Any]:
        clamped_limit = max(1, min(limit, 500))
        try:
            views, has_more = repo.list_dashboard_tasks(limit=clamped_limit)
        except TasksDashboardUnavailable as exc:
            raise HTTPException(status_code=503, detail={"code": "unavailable"}) from exc
        return {
            "available": True,
            "tasks": [_dashboard_task_to_dict(v) for v in views],
            "has_more": has_more,
        }

    return app


class TestDashboardRoute:
    def test_returns_200_with_tasks_and_has_more(self) -> None:
        app = _make_dashboard_app(_FakeDashboardRepoAvailable())
        client = TestClient(app)

        r = client.get("/api/v1/tasks/dashboard")

        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["has_more"] is False
        task = body["tasks"][0]
        assert task["task_id"] == "t1"
        assert task["source"] == "enterprise"
        assert task["approval_ids"] == ["p1"]
        assert "requested_by" not in task
        assert "result" not in task

    def test_returns_503_not_a_false_empty_success(self) -> None:
        app = _make_dashboard_app(_FakeDashboardRepoUnavailable())
        client = TestClient(app)

        r = client.get("/api/v1/tasks/dashboard")

        assert r.status_code == 503
        assert r.json() != {"available": True, "tasks": [], "has_more": False}


# ===========================================================================
# 3. Real app — auth + end-to-end (mirrors test_api_v1_authorization.py)
# ===========================================================================


@pytest.fixture()
def real_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    spool = tmp_path / "audit-spool"
    spool.mkdir()
    monkeypatch.setenv("HERMES_SHELL_DB", str(tmp_path / "shell-state.db"))
    monkeypatch.setenv("HERMES_AUDIT_SPOOL_DIR", str(spool))

    master_key = os.urandom(32)
    from hermes.shell_server import main as shell_main

    original_vault = shell_main.SecretsVault

    class _TestVault(original_vault):  # type: ignore[valid-type]
        def __init__(self, **_: Any) -> None:
            super().__init__(master_key=master_key)

    monkeypatch.setattr(shell_main, "SecretsVault", _TestVault)

    from hermes.shell_server.main import create_app

    return create_app()


class TestDashboardRouteRealApp:
    def test_rejects_unauthenticated(self, real_app: Any) -> None:
        client = TestClient(real_app, raise_server_exceptions=False)
        resp = client.get("/api/v1/tasks/dashboard")
        assert resp.status_code == 401

    def test_503_when_daemon_never_initialized_agent_tasks(self, real_app: Any) -> None:
        """shell-server alone (no daemon has ever run) has NO agent_tasks
        table — MUST 503, never a false 'available: true, tasks: []'."""
        token = real_app.state.mint_session_token()
        client = TestClient(real_app)

        resp = client.get(
            "/api/v1/tasks/dashboard",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 503

    def test_200_once_daemon_schema_present(
        self, real_app: Any, tmp_path: Path
    ) -> None:
        db_path = Path(os.environ["HERMES_SHELL_DB"])
        conn = sqlite3.connect(str(db_path))
        ensure_tasks_schema(conn)
        conn.executescript(_DDL_PENDING_DELEGATIONS)
        conn.commit()
        conn.close()
        _insert_task(db_path, instruction="hola")

        token = real_app.state.mint_session_token()
        client = TestClient(real_app)
        resp = client.get(
            "/api/v1/tasks/dashboard",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        assert len(body["tasks"]) == 1
        assert body["tasks"][0]["source"] == "local"
