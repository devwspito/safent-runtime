"""Regression (matriz 10-sep, hallazgo B): GET /api/v1/tasks/recent returned
[] with 12 rows sitting in agent_tasks (6 timer/failed, 3 timer/pending, 3
chat_message/completed).

Root cause: __main__.py builds `ControlPlaneService(...)` WITHOUT a
`trigger_repo=` (a SEPARATE SqliteAuthorizedTriggerRepository connection is
built later, only for SchedulerTimerSource/SystemEventTriggerSource, and
never plumbed into the ControlPlaneService the D-Bus wiring calls). So
`ControlPlaneService.list_recent_tasks()` always hit its `self._trigger_repo
is None` early-return and answered `()` — in EVERY production instance, not
just a misconfigured one.

Fix: `DbusRuntimeServiceWiring.list_recent_tasks` additionally reads through
`self._require_trigger_repo()` — the SAME lazily-built repo (same
shell-state.db) every other trigger verb in this class already uses
(get_scheduled_task, delete_scheduled_task, …) — and merges those rows in,
dedup'd by task_id, alongside whatever cp_service contributes (still
supported, for callers that DO wire a trigger_repo into cp_service).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from hermes.tasks.control_plane.application.control_plane_service import ControlPlaneService
from hermes.tasks.triggers.domain.authorized_trigger_ports import AuthorizedTriggerType, RiskCeiling
from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (
    SqliteAuthorizedTriggerRepository,
)

pytestmark = pytest.mark.unit

_ADMIN_UUID = uuid4()


class _FakeQueue:
    def all_items(self) -> list:
        return []


class _FakeAgentState:
    async def is_paused(self) -> bool:
        return False

    async def pause(self, **_: object) -> None:
        pass

    async def resume(self, **_: object) -> None:
        pass


class _FakeApprovalGate:
    async def approve(self, *, proposal_id: UUID, approved_by: UUID) -> str:
        return f"token-{proposal_id}"

    async def reject(self, *, proposal_id: UUID, rejected_by: UUID, reason: str) -> None:
        pass


def _insert_agent_task(
    conn: sqlite3.Connection,
    *,
    trigger_instance_id: str | None,
    status: str,
    instruction: str = "run report",
) -> str:
    task_id = str(uuid4())
    now = datetime.now(tz=UTC).isoformat()
    admin_str = str(_ADMIN_UUID)
    payload = json.dumps({"enqueued_by": admin_str, "instruction": instruction})
    trigger_kind = "timer" if trigger_instance_id is not None else "manual_enqueue"
    conn.execute(
        """
        INSERT INTO agent_tasks (
            task_id, trigger_kind, enqueued_by, operator_id,
            instruction, payload_json, status,
            kind, priority, retry_count, max_retries,
            created_at, updated_at,
            trigger_instance_id, tenant_id, worker_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'autonomous', 0, 1, 3, ?, ?, ?, ?, ?)
        """,
        (
            task_id, trigger_kind, admin_str, admin_str,
            instruction, payload, status,
            now, now,
            trigger_instance_id, str(uuid4()), "worker-0",
        ),
    )
    conn.commit()
    return task_id


async def _seed_timer_trigger(repo: SqliteAuthorizedTriggerRepository) -> UUID:
    trigger = await repo.authorize(
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value="0 * * * *",
        allowed_capabilities=("list_services",),
        risk_ceiling=RiskCeiling.LOW,
        admin_uuid=_ADMIN_UUID,
        approval_signature="test-sig",
    )
    return trigger.trigger_instance_id


def _make_wiring(*, cp_service, trigger_repo):
    from hermes.agents_os.infrastructure.dbus_runtime_service import DbusRuntimeServiceWiring
    from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_FakeApprovalGate(),
        authorized_uids=frozenset({os.getuid()}),
        control_plane_service=cp_service,
        trigger_repo=trigger_repo,
    )


class TestListRecentTasksProductionWiring:
    async def test_cp_service_without_trigger_repo_no_longer_hides_agent_tasks_rows(self) -> None:
        """Pins the exact matrix symptom: cp_service exists (like production)
        but was built with trigger_repo=None, while agent_tasks has real
        rows — /tasks/recent must NOT be []."""
        repo = SqliteAuthorizedTriggerRepository.in_memory()
        trigger_id = await _seed_timer_trigger(repo)
        _insert_agent_task(repo._conn, trigger_instance_id=str(trigger_id), status="failed")  # noqa: SLF001
        _insert_agent_task(repo._conn, trigger_instance_id=None, status="pending")  # noqa: SLF001

        # Mirrors __main__.py exactly: ControlPlaneService with NO trigger_repo.
        cp_service = ControlPlaneService(
            queue=_FakeQueue(),
            agent_state=_FakeAgentState(),
            authorized_uids=frozenset({os.getuid()}),
            tenant_id=uuid4(),
        )
        wiring = _make_wiring(cp_service=cp_service, trigger_repo=repo)

        result = await wiring.list_recent_tasks()

        assert result != []
        statuses = {r["status"] for r in result}
        assert statuses == {"failed", "pending"}

    async def test_no_cp_service_at_all_still_surfaces_agent_tasks_rows(self) -> None:
        repo = SqliteAuthorizedTriggerRepository.in_memory()
        _insert_agent_task(repo._conn, trigger_instance_id=None, status="failed")  # noqa: SLF001
        wiring = _make_wiring(cp_service=None, trigger_repo=repo)

        result = await wiring.list_recent_tasks()

        assert len(result) == 1
        assert result[0]["status"] == "failed"

    async def test_cp_service_rows_and_repo_rows_do_not_duplicate(self) -> None:
        """When cp_service DOES have its own working trigger_repo pointed at
        the SAME db as this wiring's trigger_repo, the row must appear once,
        not twice."""
        repo = SqliteAuthorizedTriggerRepository.in_memory()
        _insert_agent_task(repo._conn, trigger_instance_id=None, status="failed")  # noqa: SLF001

        cp_service = ControlPlaneService(
            queue=_FakeQueue(),
            agent_state=_FakeAgentState(),
            authorized_uids=frozenset({os.getuid()}),
            tenant_id=uuid4(),
            trigger_repo=repo,
        )
        wiring = _make_wiring(cp_service=cp_service, trigger_repo=repo)

        result = await wiring.list_recent_tasks()

        assert len(result) == 1
