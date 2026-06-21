"""T-G3-INT — Integration: schedule_task ↔ SqliteAuthorizedTriggerRepository round-trip.

Condition 3 of the feature-007 GATE:
  - schedule_task with injected trigger_repo persists an enabled timer entry.
  - list_scheduled_tasks returns that entry via the same repo.
  - unschedule_task revokes the entry (list_scheduled_tasks no longer includes it).

All assertions use the REAL SqliteAuthorizedTriggerRepository in-memory factory.
No mocks beyond OsNativeDispatcher's trigger_repo injection.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (
    SqliteAuthorizedTriggerRepository,
)

pytestmark = pytest.mark.integration

_ADMIN_UUID = str(uuid4())
_SIGNATURE = "test-approval-signature"


@pytest.fixture()
def repo() -> SqliteAuthorizedTriggerRepository:
    return SqliteAuthorizedTriggerRepository.in_memory()


@pytest.fixture()
def dispatcher(repo: SqliteAuthorizedTriggerRepository) -> OsNativeDispatcher:
    return OsNativeDispatcher(trigger_repo=repo)


class TestScheduleTaskRoundTrip:
    """schedule_task → list_scheduled_tasks → unschedule_task round-trip (Condition 3)."""

    async def test_schedule_task_persists_timer_entry(
        self, dispatcher: OsNativeDispatcher
    ) -> None:
        """schedule_task with injected repo creates an enabled authorized_trigger_instances row."""
        result = await dispatcher.execute(
            skill_name="schedule_task",
            args={
                "trigger_type": "timer",
                "schedule": "0 * * * *",
                "capability_scope": "list_services",
                "admin_uuid": _ADMIN_UUID,
                "approval_signature": _SIGNATURE,
                "reason": "integration test",
            },
        )

        assert result["ok"] is True, f"schedule_task must succeed: {result}"
        assert "trigger_instance_id" in result, (
            "schedule_task must return trigger_instance_id (the allow-list entry UUID)"
        )

    async def test_list_scheduled_tasks_returns_persisted_entry(
        self, dispatcher: OsNativeDispatcher
    ) -> None:
        """After schedule_task, list_scheduled_tasks includes the new entry."""
        schedule_result = await dispatcher.execute(
            skill_name="schedule_task",
            args={
                "trigger_type": "timer",
                "schedule": "0 9 * * 1",
                "capability_scope": "get_system_info",
                "admin_uuid": _ADMIN_UUID,
                "approval_signature": _SIGNATURE,
            },
        )
        assert schedule_result["ok"] is True

        list_result = await dispatcher.execute(
            skill_name="list_scheduled_tasks",
            args={},
        )

        assert list_result["ok"] is True, f"list_scheduled_tasks failed: {list_result}"
        scheduled = list_result.get("scheduled", [])
        assert len(scheduled) >= 1, (
            "list_scheduled_tasks must return at least the entry just created"
        )
        instance_ids = [s["trigger_instance_id"] for s in scheduled]
        assert schedule_result["trigger_instance_id"] in instance_ids, (
            "The created trigger_instance_id must appear in list_scheduled_tasks"
        )

    async def test_unschedule_task_revokes_entry(
        self, dispatcher: OsNativeDispatcher
    ) -> None:
        """unschedule_task revokes the entry — list_scheduled_tasks no longer returns it."""
        schedule_result = await dispatcher.execute(
            skill_name="schedule_task",
            args={
                "trigger_type": "timer",
                "schedule": "30 6 * * *",
                "capability_scope": "list_devices",
                "admin_uuid": _ADMIN_UUID,
                "approval_signature": _SIGNATURE,
            },
        )
        assert schedule_result["ok"] is True
        instance_id = schedule_result["trigger_instance_id"]

        unschedule_result = await dispatcher.execute(
            skill_name="unschedule_task",
            args={
                "trigger_instance_id": instance_id,
                "admin_uuid": _ADMIN_UUID,
                "reason": "integration test teardown",
            },
        )
        assert unschedule_result["ok"] is True, (
            f"unschedule_task must succeed: {unschedule_result}"
        )

        list_result = await dispatcher.execute(
            skill_name="list_scheduled_tasks",
            args={},
        )
        scheduled = list_result.get("scheduled", [])
        remaining_ids = [s["trigger_instance_id"] for s in scheduled]
        assert instance_id not in remaining_ids, (
            "Revoked trigger instance must NOT appear in list_scheduled_tasks"
        )

    async def test_schedule_task_without_repo_fails_closed(self) -> None:
        """Without injected repo, schedule_task returns ok=False (fail-closed)."""
        dispatcher_no_repo = OsNativeDispatcher()

        result = await dispatcher_no_repo.execute(
            skill_name="schedule_task",
            args={
                "trigger_type": "timer",
                "schedule": "0 * * * *",
                "capability_scope": "list_services",
            },
        )

        assert result["ok"] is False, (
            "schedule_task without trigger_repo must return ok=False (fail-closed). "
            f"Got: {result}"
        )

    async def test_unschedule_task_without_repo_fails_closed(self) -> None:
        """Without injected repo, unschedule_task returns ok=False (fail-closed)."""
        dispatcher_no_repo = OsNativeDispatcher()

        result = await dispatcher_no_repo.execute(
            skill_name="unschedule_task",
            args={"trigger_instance_id": str(uuid4())},
        )

        assert result["ok"] is False, (
            "unschedule_task without trigger_repo must return ok=False (fail-closed). "
            f"Got: {result}"
        )

    async def test_list_scheduled_tasks_without_repo_returns_empty(self) -> None:
        """Without injected repo, list_scheduled_tasks returns ok=True with empty list."""
        dispatcher_no_repo = OsNativeDispatcher()

        result = await dispatcher_no_repo.execute(
            skill_name="list_scheduled_tasks",
            args={},
        )

        assert result["ok"] is True, (
            "list_scheduled_tasks without trigger_repo must return ok=True (interface stable)"
        )
        assert result.get("scheduled") == [], (
            "list_scheduled_tasks without trigger_repo must return empty scheduled list"
        )

    async def test_schedule_task_invalid_trigger_type_rejected(
        self, dispatcher: OsNativeDispatcher
    ) -> None:
        """Only 'timer' trigger_type is accepted (FR-010)."""
        result = await dispatcher.execute(
            skill_name="schedule_task",
            args={
                "trigger_type": "systemd_unit",
                "schedule": "0 * * * *",
                "capability_scope": "list_services",
            },
        )

        assert result["ok"] is False
        assert "timer" in result.get("reason", "").lower(), (
            "Error message must mention 'timer' as the only supported type"
        )

    async def test_schedule_task_missing_schedule_rejected(
        self, dispatcher: OsNativeDispatcher
    ) -> None:
        """Missing 'schedule' parameter returns ok=False."""
        result = await dispatcher.execute(
            skill_name="schedule_task",
            args={
                "trigger_type": "timer",
                "capability_scope": "list_services",
            },
        )

        assert result["ok"] is False
        assert "schedule" in result.get("reason", ""), (
            "Error must mention missing 'schedule' parameter"
        )
