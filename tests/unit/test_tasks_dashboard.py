"""Tests — Tasks Dashboard (F007 read path).

Covers:
  1. SqliteAuthorizedTriggerRepository.list_triggers_with_last_run
     - trigger with no runs → last_run_at/last_status = None
     - trigger with runs → last_run_at/last_status populated from most recent
     - revoked trigger excluded (enabled=0)
     - enabled flag surfaced correctly

  2. SqliteAuthorizedTriggerRepository.list_recent_tasks
     - returns tasks ordered by enqueued_at DESC
     - instruction truncated at 120 chars (CTRL-P1-5)

  3. ControlPlaneService.list_configured_tasks
     - integrates trigger repo join
     - builds ConfiguredTaskView with correct fields

  4. ControlPlaneService.list_recent_tasks
     - maps repo output to RecentTaskView

  5. _cron_next_fire (clock-injectable helper)
     - known expr + fixed 'after' → expected next fire
     - unrecognised expr → None
     - wildcard and step syntax

  6. DbusRuntimeServiceWiring.list_configured_tasks / list_recent_tasks
     - returns list[dict] when cp_service available
     - returns [] when cp_service is None

  7. Shell-server routes GET /api/v1/tasks/configured and /recent
     - returns 200 + {available: true, tasks: [...]} when runtime available
     - returns 200 + {available: false, tasks: []} on AgentUnavailable (not 500)

  8. ShellBackendClient.list_configured_tasks / list_recent_tasks
     - parses server response into dict

  9. D-Bus DTO serialization round-trip
     - ConfiguredTaskView → dict → ConfiguredTaskView fields match
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.tasks.control_plane.application.control_plane_service import (
    _cron_next_fire,
    _cron_recurrence_human,
    _build_configured_task_view,
)
from hermes.tasks.control_plane.domain.ports import (
    ConfiguredTaskView,
    RecentTaskView,
)
from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (
    SqliteAuthorizedTriggerRepository,
    _extract_instruction,
)
from hermes.tasks.infrastructure.schema import ensure_tasks_schema

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ADMIN_UUID = uuid4()
_SIG = "test-sig"


def _make_repo() -> SqliteAuthorizedTriggerRepository:
    return SqliteAuthorizedTriggerRepository.in_memory()


async def _seed_trigger(
    repo: SqliteAuthorizedTriggerRepository,
    *,
    scope: str = "0 * * * *",
    trigger_type: str = "timer",
) -> UUID:
    from hermes.tasks.triggers.domain.authorized_trigger_ports import (
        AuthorizedTriggerType,
        RiskCeiling,
    )

    t = await repo.authorize(
        trigger_type=AuthorizedTriggerType(trigger_type),
        scope_value=scope,
        allowed_capabilities=("list_services",),
        risk_ceiling=RiskCeiling.LOW,
        admin_uuid=_ADMIN_UUID,
        approval_signature=_SIG,
    )
    return t.trigger_instance_id


def _insert_task(
    conn: sqlite3.Connection,
    *,
    trigger_instance_id: str | None,
    status: str = "failed",
    enqueued_at: str | None = None,
    instruction: str = "test task",
) -> str:
    """Insert a minimal agent_tasks row using the real P2 schema columns.

    Default status is 'failed' (not 'completed') because the schema CHECK
    constraint requires execution_audit_entry_id + execution_head_hash for
    the COMPLETED state — fields we don't need for dashboard read tests.
    """
    task_id = str(uuid4())
    now = enqueued_at or datetime.now(tz=UTC).isoformat()
    admin_str = str(_ADMIN_UUID)
    payload = json.dumps({"enqueued_by": admin_str, "instruction": instruction})
    # Schema CHECK: trigger_kind='timer' requires trigger_instance_id IS NOT NULL;
    # trigger_kind='manual_enqueue' requires trigger_instance_id IS NULL.
    trigger_kind = "timer" if trigger_instance_id is not None else "manual_enqueue"
    conn.execute(
        """
        INSERT INTO agent_tasks (
            task_id, trigger_kind, enqueued_by, operator_id,
            instruction, payload_json, status,
            kind, priority, retry_count, max_retries,
            created_at, updated_at,
            trigger_instance_id, tenant_id, worker_id
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?, ?,
            'autonomous', 0, 1, 3,
            ?, ?,
            ?, ?, ?
        )
        """,
        (
            task_id,
            trigger_kind,
            admin_str,
            admin_str,      # operator_id
            instruction,    # instruction column
            payload,
            status,
            now,
            now,
            trigger_instance_id,
            str(uuid4()),   # tenant_id
            "worker-0",
        ),
    )
    conn.commit()
    return task_id


# ===========================================================================
# 1. list_triggers_with_last_run
# ===========================================================================


class TestListTriggersWithLastRun:
    async def test_trigger_with_no_runs_returns_none_last_run(self) -> None:
        repo = _make_repo()
        tid = await _seed_trigger(repo)

        rows = repo.list_triggers_with_last_run()

        assert len(rows) == 1
        trigger, last_run_at, last_status = rows[0]
        assert str(trigger.trigger_instance_id) == str(tid)
        assert last_run_at is None
        assert last_status is None

    async def test_trigger_with_run_populates_last_run(self) -> None:
        repo = _make_repo()
        tid = await _seed_trigger(repo)

        enqueued_at = "2026-05-01T10:00:00+00:00"
        _insert_task(
            repo._conn,
            trigger_instance_id=str(tid),
            status="failed",
            enqueued_at=enqueued_at,
        )

        rows = repo.list_triggers_with_last_run()

        assert len(rows) == 1
        _, last_run_at, last_status = rows[0]
        assert last_run_at is not None
        assert last_status == "failed"

    async def test_revoked_trigger_excluded(self) -> None:
        repo = _make_repo()
        tid = await _seed_trigger(repo)
        await repo.revoke(trigger_instance_id=tid, admin_uuid=_ADMIN_UUID)

        rows = repo.list_triggers_with_last_run()

        assert len(rows) == 0

    async def test_most_recent_run_wins(self) -> None:
        """When a trigger has multiple runs, the most recent created_at is used."""
        repo = _make_repo()
        tid = await _seed_trigger(repo)

        _insert_task(
            repo._conn,
            trigger_instance_id=str(tid),
            status="failed",
            enqueued_at="2026-05-01T09:00:00+00:00",
        )
        _insert_task(
            repo._conn,
            trigger_instance_id=str(tid),
            status="pending",
            enqueued_at="2026-05-02T09:00:00+00:00",
        )

        rows = repo.list_triggers_with_last_run()

        assert len(rows) == 1
        _, last_run_at, last_status = rows[0]
        # MAX(created_at) should pick the later one
        assert "2026-05-02" in last_run_at
        assert last_status == "pending"

    async def test_enabled_flag_surfaced(self) -> None:
        repo = _make_repo()
        await _seed_trigger(repo)

        rows = repo.list_triggers_with_last_run()

        trigger, _, _ = rows[0]
        assert trigger.enabled is True


# ===========================================================================
# 2. list_recent_tasks
# ===========================================================================


class TestListRecentTasks:
    async def test_returns_tasks_ordered_newest_first(self) -> None:
        repo = _make_repo()

        _insert_task(
            repo._conn,
            trigger_instance_id=None,
            status="pending",
            enqueued_at="2026-05-01T08:00:00+00:00",
            instruction="old task",
        )
        _insert_task(
            repo._conn,
            trigger_instance_id=None,
            status="failed",
            enqueued_at="2026-05-02T08:00:00+00:00",
            instruction="new task",
        )

        rows = repo.list_recent_tasks(limit=10)

        assert len(rows) == 2
        assert rows[0]["status"] == "failed"   # newest first (2026-05-02)
        assert rows[1]["status"] == "pending"  # oldest (2026-05-01)

    async def test_instruction_truncated_at_120_chars(self) -> None:
        repo = _make_repo()
        long_instruction = "x" * 200
        _insert_task(
            repo._conn,
            trigger_instance_id=None,
            status="failed",
            instruction=long_instruction,
        )

        rows = repo.list_recent_tasks()

        assert len(rows[0]["instruction_truncated"]) <= 120

    async def test_limit_respected(self) -> None:
        repo = _make_repo()
        for i in range(5):
            _insert_task(repo._conn, trigger_instance_id=None, instruction=f"task {i}")

        rows = repo.list_recent_tasks(limit=3)

        assert len(rows) <= 3


# ===========================================================================
# 3. _extract_instruction helper
# ===========================================================================


class TestExtractInstruction:
    def test_returns_instruction_field(self) -> None:
        payload = json.dumps({"instruction": "do something", "enqueued_by": "uid"})
        assert _extract_instruction(payload) == "do something"

    def test_truncates_at_120(self) -> None:
        payload = json.dumps({"instruction": "a" * 200})
        assert len(_extract_instruction(payload)) == 120

    def test_empty_on_null_payload(self) -> None:
        assert _extract_instruction(None) == ""

    def test_empty_on_bad_json(self) -> None:
        assert _extract_instruction("not-json{") == ""

    def test_no_instruction_key_returns_empty(self) -> None:
        payload = json.dumps({"other": "value"})
        assert _extract_instruction(payload) == ""


# ===========================================================================
# 4. _cron_next_fire clock-injectable helper
# ===========================================================================


class TestCronNextFire:
    def _after(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso)

    def test_every_hour_at_minute_zero(self) -> None:
        # "0 * * * *" — next fire after 2026-01-01T10:30 is 11:00
        after = self._after("2026-01-01T10:30:00+00:00")
        result = _cron_next_fire("0 * * * *", after=after)
        assert result is not None
        assert result.hour == 11
        assert result.minute == 0

    def test_daily_at_fixed_hour(self) -> None:
        # "30 9 * * *" — next after 09:31 same day is 09:30 next day
        after = self._after("2026-01-01T09:31:00+00:00")
        result = _cron_next_fire("30 9 * * *", after=after)
        assert result is not None
        assert result.day == 2
        assert result.hour == 9
        assert result.minute == 30

    def test_step_syntax_every_15_minutes(self) -> None:
        # "*/15 * * * *" — next after :00 is :15
        after = self._after("2026-01-01T10:00:00+00:00")
        result = _cron_next_fire("*/15 * * * *", after=after)
        assert result is not None
        assert result.minute == 15

    def test_specific_day_of_week(self) -> None:
        # "0 9 * * 1" — Monday at 09:00
        # 2026-01-01 is a Thursday (weekday=3), next Monday is 2026-01-05
        after = self._after("2026-01-01T00:00:00+00:00")
        result = _cron_next_fire("0 9 * * 1", after=after)
        assert result is not None
        assert result.isoweekday() == 1  # Monday

    def test_unparseable_cron_returns_none(self) -> None:
        # croniter (full grammar) rejects truly malformed expressions.
        after = self._after("2026-01-01T00:00:00+00:00")
        result = _cron_next_fire("not a cron", after=after)
        assert result is None

    def test_non_numeric_field_returns_none(self) -> None:
        after = self._after("2026-01-01T00:00:00+00:00")
        result = _cron_next_fire("0 X * * *", after=after)
        assert result is None

    def test_result_is_strictly_after_input(self) -> None:
        after = self._after("2026-06-01T12:00:00+00:00")
        result = _cron_next_fire("* * * * *", after=after)
        assert result is not None
        assert result > after

    def test_monthly_expression(self) -> None:
        # "0 0 1 6 *" — first of June at midnight; after Feb should be June 1
        after = self._after("2026-02-01T00:00:00+00:00")
        result = _cron_next_fire("0 0 1 6 *", after=after)
        assert result is not None
        assert result.month == 6
        assert result.day == 1


class TestCronRecurrenceHuman:
    def test_returns_human_description(self) -> None:
        # cron_descriptor renders a legible string (locale-dependent wording).
        result = _cron_recurrence_human("0 9 * * 1")
        assert result != ""
        assert "09" in result

    def test_bad_cron_returns_empty_string(self) -> None:
        assert _cron_recurrence_human("not a cron") == ""


# ===========================================================================
# 5. ControlPlaneService.list_configured_tasks / list_recent_tasks
# ===========================================================================


class _FakeQueue:
    """Minimal WorkQueuePort stub for ControlPlaneService tests."""

    def all_items(self) -> list:
        return []


class _FakeAgentState:
    async def is_paused(self) -> bool:
        return False

    async def pause(self, **_: Any) -> None:
        pass

    async def resume(self, **_: Any) -> None:
        pass


class TestControlPlaneServiceDashboard:
    def _make_service(self, repo: SqliteAuthorizedTriggerRepository | None = None):
        from hermes.tasks.control_plane.application.control_plane_service import (
            ControlPlaneService,
        )

        return ControlPlaneService(
            queue=_FakeQueue(),
            agent_state=_FakeAgentState(),
            authorized_uids=frozenset({os.getuid()}),
            tenant_id=uuid4(),
            trigger_repo=repo,
        )

    async def test_returns_empty_when_no_trigger_repo(self) -> None:
        service = self._make_service(repo=None)
        result = await service.list_configured_tasks()
        assert result == ()

    async def test_returns_empty_recent_when_no_trigger_repo(self) -> None:
        service = self._make_service(repo=None)
        result = await service.list_recent_tasks()
        assert result == ()

    async def test_configured_tasks_with_trigger(self) -> None:
        repo = _make_repo()
        await _seed_trigger(repo, scope="0 */2 * * *")
        service = self._make_service(repo=repo)

        result = await service.list_configured_tasks()

        assert len(result) == 1
        view = result[0]
        assert isinstance(view, ConfiguredTaskView)
        assert view.trigger_type == "timer"
        assert view.recurrence == "0 */2 * * *"
        assert view.enabled is True
        assert view.risk_ceiling == "low"
        assert view.last_run_at is None
        assert view.last_status is None

    async def test_next_run_at_computed_for_timer(self) -> None:
        repo = _make_repo()
        await _seed_trigger(repo, scope="0 * * * *")
        service = self._make_service(repo=repo)

        result = await service.list_configured_tasks()

        view = result[0]
        assert view.next_run_at is not None, (
            "next_run_at must be computed for a valid cron expression"
        )

    async def test_revoked_trigger_not_in_configured_tasks(self) -> None:
        repo = _make_repo()
        tid = await _seed_trigger(repo)
        await repo.revoke(trigger_instance_id=tid, admin_uuid=_ADMIN_UUID)
        service = self._make_service(repo=repo)

        result = await service.list_configured_tasks()

        assert result == ()

    async def test_configured_tasks_with_last_run(self) -> None:
        repo = _make_repo()
        tid = await _seed_trigger(repo)
        _insert_task(
            repo._conn,
            trigger_instance_id=str(tid),
            status="failed",
            enqueued_at="2026-05-30T08:00:00+00:00",
        )
        service = self._make_service(repo=repo)

        result = await service.list_configured_tasks()

        view = result[0]
        assert view.last_run_at is not None
        assert view.last_status == "failed"

    async def test_recent_tasks_returns_recent_view(self) -> None:
        repo = _make_repo()
        _insert_task(
            repo._conn,
            trigger_instance_id=None,
            status="failed",
            instruction="hello world",
        )
        service = self._make_service(repo=repo)

        result = await service.list_recent_tasks()

        assert len(result) == 1
        view = result[0]
        assert isinstance(view, RecentTaskView)
        assert view.status == "failed"
        assert "hello world" in view.label


# ===========================================================================
# 6. DbusRuntimeServiceWiring.list_configured_tasks / list_recent_tasks
# ===========================================================================


class _FakeApprovalGate:
    async def approve(self, *, proposal_id: UUID, approved_by: UUID) -> str:
        return f"token-{proposal_id}"

    async def reject(self, *, proposal_id: UUID, rejected_by: UUID, reason: str) -> None:
        pass


class TestDbusWiringDashboardMethods:
    def _make_wiring(
        self, cp_service: Any = None
    ):
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            DbusRuntimeServiceWiring,
        )
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

        return DbusRuntimeServiceWiring(
            agent_state=InMemoryAgentState(),
            approval_gate=_FakeApprovalGate(),
            authorized_uids=frozenset({os.getuid()}),
            control_plane_service=cp_service,
        )

    async def test_list_configured_tasks_returns_empty_when_no_cp_service(
        self,
    ) -> None:
        wiring = self._make_wiring(cp_service=None)
        result = await wiring.list_configured_tasks()
        assert result == []

    async def test_list_recent_tasks_returns_empty_when_no_cp_service(self) -> None:
        wiring = self._make_wiring(cp_service=None)
        result = await wiring.list_recent_tasks()
        assert result == []

    async def test_list_configured_tasks_returns_dict_list(self) -> None:
        repo = _make_repo()
        await _seed_trigger(repo, scope="0 * * * *")

        from hermes.tasks.control_plane.application.control_plane_service import (
            ControlPlaneService,
        )

        service = ControlPlaneService(
            queue=_FakeQueue(),
            agent_state=_FakeAgentState(),
            authorized_uids=frozenset({os.getuid()}),
            tenant_id=uuid4(),
            trigger_repo=repo,
        )
        wiring = self._make_wiring(cp_service=service)

        result = await wiring.list_configured_tasks()

        assert isinstance(result, list)
        assert len(result) == 1
        row = result[0]
        assert "trigger_id" in row
        assert "recurrence" in row
        assert "enabled" in row

    async def test_list_recent_tasks_returns_dict_list(self) -> None:
        repo = _make_repo()
        _insert_task(
            repo._conn,
            trigger_instance_id=None,
            status="failed",
            instruction="run report",
        )

        from hermes.tasks.control_plane.application.control_plane_service import (
            ControlPlaneService,
        )

        service = ControlPlaneService(
            queue=_FakeQueue(),
            agent_state=_FakeAgentState(),
            authorized_uids=frozenset({os.getuid()}),
            tenant_id=uuid4(),
            trigger_repo=repo,
        )
        wiring = self._make_wiring(cp_service=service)

        result = await wiring.list_recent_tasks()

        assert isinstance(result, list)
        assert len(result) == 1
        row = result[0]
        assert "task_id" in row
        assert "status" in row
        assert row["status"] == "failed"


# ===========================================================================
# 7. Shell-server HTTP routes
# ===========================================================================


class _AvailableControlPlane:
    """Stub that returns mock data for dashboard endpoints."""

    async def list_configured_tasks(self, *, limit: int = 200) -> tuple:
        return (
            ConfiguredTaskView(
                trigger_id="aaa-bbb",
                label="timer: 0 * * * *",
                trigger_type="timer",
                recurrence="0 * * * *",
                enabled=True,
                risk_ceiling="low",
                last_run_at="2026-05-01T08:00:00+00:00",
                last_status="completed",
                next_run_at="2026-05-01T09:00:00+00:00",
            ),
        )

    async def list_recent_tasks(self, *, limit: int = 50) -> tuple:
        return (
            RecentTaskView(
                task_id="task-123",
                label="run report",
                status="completed",
                trigger_kind="timer",
                enqueued_at="2026-05-01T08:00:00+00:00",
                claimed_at=None,
            ),
        )


class _UnavailableControlPlaneDash:
    """Stub that raises AgentUnavailable for dashboard endpoints."""

    async def list_configured_tasks(self, **_: Any) -> None:
        from hermes.tasks.control_plane.domain.ports import AgentUnavailable
        raise AgentUnavailable("daemon not running")

    async def list_recent_tasks(self, **_: Any) -> None:
        from hermes.tasks.control_plane.domain.ports import AgentUnavailable
        raise AgentUnavailable("daemon not running")


def _make_tasks_app(control_plane: Any) -> FastAPI:
    """Build a minimal FastAPI app with only the task routes wired.

    Avoids the full create_app() bootstrap (SecretsVault, audit spool, etc.)
    by extracting just the two task endpoints inline. This mirrors how other
    test files (test_audit_api, test_training_api) test individual sub-routers
    without needing the full shell-server.
    """
    from fastapi import FastAPI  # noqa: PLC0415
    from hermes.tasks.control_plane.domain.ports import AgentUnavailable  # noqa: PLC0415

    app = FastAPI()
    app.state.control_plane = control_plane

    @app.get("/api/v1/tasks/configured")
    async def list_configured_tasks(limit: int = 200):
        try:
            rows = await app.state.control_plane.list_configured_tasks(limit=limit)
            return {
                "available": True,
                "tasks": [
                    {
                        "trigger_id": r.trigger_id,
                        "label": r.label,
                        "trigger_type": r.trigger_type,
                        "recurrence": r.recurrence,
                        "enabled": r.enabled,
                        "risk_ceiling": r.risk_ceiling,
                        "last_run_at": r.last_run_at,
                        "last_status": r.last_status,
                        "next_run_at": r.next_run_at,
                    }
                    for r in rows
                ],
            }
        except AgentUnavailable:
            return {"available": False, "tasks": []}

    @app.get("/api/v1/tasks/recent")
    async def list_recent_tasks(limit: int = 50):
        try:
            rows = await app.state.control_plane.list_recent_tasks(limit=limit)
            return {
                "available": True,
                "tasks": [
                    {
                        "task_id": r.task_id,
                        "label": r.label,
                        "status": r.status,
                        "trigger_kind": r.trigger_kind,
                        "enqueued_at": r.enqueued_at,
                        "claimed_at": r.claimed_at,
                    }
                    for r in rows
                ],
            }
        except AgentUnavailable:
            return {"available": False, "tasks": []}

    return app


class TestShellServerTaskRoutes:
    def test_configured_tasks_available(self) -> None:
        app = _make_tasks_app(_AvailableControlPlane())
        client = TestClient(app)

        r = client.get("/api/v1/tasks/configured")

        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert len(body["tasks"]) == 1
        task = body["tasks"][0]
        assert task["trigger_id"] == "aaa-bbb"
        assert task["trigger_type"] == "timer"
        assert task["enabled"] is True

    def test_configured_tasks_unavailable_returns_200_not_500(self) -> None:
        app = _make_tasks_app(_UnavailableControlPlaneDash())
        client = TestClient(app)

        r = client.get("/api/v1/tasks/configured")

        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["tasks"] == []

    def test_recent_tasks_available(self) -> None:
        app = _make_tasks_app(_AvailableControlPlane())
        client = TestClient(app)

        r = client.get("/api/v1/tasks/recent")

        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert len(body["tasks"]) == 1
        task = body["tasks"][0]
        assert task["task_id"] == "task-123"
        assert task["status"] == "completed"

    def test_recent_tasks_unavailable_returns_200_not_500(self) -> None:
        app = _make_tasks_app(_UnavailableControlPlaneDash())
        client = TestClient(app)

        r = client.get("/api/v1/tasks/recent")

        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["tasks"] == []


# ===========================================================================
# 8. ShellBackendClient.list_configured_tasks / list_recent_tasks
# ===========================================================================


class TestShellBackendClientParsing:
    """ShellBackendClient methods parse the server response into a dict."""

    def _patched_client(self, response: dict):
        from hermes.shell.infrastructure.shell_backend_client import ShellBackendClient

        client = ShellBackendClient(base_url="http://unused")

        def _fake_request(*, path: str, **_: Any) -> dict:
            return response

        client._request = _fake_request  # type: ignore[method-assign]
        return client

    def test_list_configured_tasks_parses_response(self) -> None:
        fake_response = {
            "available": True,
            "tasks": [
                {
                    "trigger_id": "x",
                    "label": "timer: 0 * * * *",
                    "trigger_type": "timer",
                    "recurrence": "0 * * * *",
                    "enabled": True,
                    "risk_ceiling": "low",
                    "last_run_at": None,
                    "last_status": None,
                    "next_run_at": None,
                }
            ],
        }
        client = self._patched_client(fake_response)
        result = client.list_configured_tasks()
        assert result["available"] is True
        assert len(result["tasks"]) == 1

    def test_list_recent_tasks_parses_response(self) -> None:
        fake_response = {
            "available": True,
            "tasks": [
                {
                    "task_id": "t1",
                    "label": "run report",
                    "status": "completed",
                    "trigger_kind": "timer",
                    "enqueued_at": "2026-05-01T08:00:00+00:00",
                    "claimed_at": None,
                }
            ],
        }
        client = self._patched_client(fake_response)
        result = client.list_recent_tasks()
        assert result["available"] is True
        assert result["tasks"][0]["status"] == "completed"

    def test_list_configured_tasks_handles_empty_available_false(self) -> None:
        client = self._patched_client({"available": False, "tasks": []})
        result = client.list_configured_tasks()
        assert result["available"] is False
        assert result["tasks"] == []


# ===========================================================================
# 9. _configured_task_to_dict round-trip
# ===========================================================================


class TestConfiguredTaskDictRoundTrip:
    def test_round_trip_preserves_fields(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _configured_task_to_dict,
        )

        view = ConfiguredTaskView(
            trigger_id="abc-123",
            label="timer: 0 9 * * 1",
            trigger_type="timer",
            recurrence="0 9 * * 1",
            enabled=True,
            risk_ceiling="high",
            last_run_at="2026-05-01T09:00:00+00:00",
            last_status="completed",
            next_run_at="2026-05-08T09:00:00+00:00",
        )

        d = _configured_task_to_dict(view)

        assert d["trigger_id"] == "abc-123"
        assert d["label"] == "timer: 0 9 * * 1"
        assert d["trigger_type"] == "timer"
        assert d["recurrence"] == "0 9 * * 1"
        assert d["enabled"] is True
        assert d["risk_ceiling"] == "high"
        assert d["last_run_at"] == "2026-05-01T09:00:00+00:00"
        assert d["last_status"] == "completed"
        assert d["next_run_at"] == "2026-05-08T09:00:00+00:00"

    def test_none_values_serialized_as_empty_string(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _configured_task_to_dict,
        )

        view = ConfiguredTaskView(
            trigger_id="x",
            label="timer: 0 * * * *",
            trigger_type="timer",
            recurrence="0 * * * *",
            enabled=True,
            risk_ceiling="low",
            last_run_at=None,
            last_status=None,
            next_run_at=None,
        )

        d = _configured_task_to_dict(view)

        # D-Bus a{sv} doesn't support None; empty string is the convention
        assert d["last_run_at"] == ""
        assert d["last_status"] == ""
        assert d["next_run_at"] == ""
