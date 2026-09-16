"""Regression tests — specs/025-safent-repaso hallazgo #6 ("Cron: ids
incompatibles lista/detalle y recent vacío").

Root causes:
  1. `_neus_job_to_task_dict` (the mapper `list_configured_tasks` uses — it
     reads Neus cron.jobs, not the Safent trigger repo, since BUG-7) always
     returned the Neus job's OWN short id as `trigger_id`, even for jobs
     created through Safent's create_scheduled_task — which stamps the real
     authorized_trigger UUID onto `job["origin"]["trigger_instance_id"]`
     specifically so it can be read back here. `get_scheduled_task` /
     `set_scheduled_task_enabled` / `delete_scheduled_task` all do
     `UUID(trigger_id)` against that Safent table, so the dashboard's own
     listing fed them an id they could never parse: detail 404, toggle
     `{"ok": false, "error": "trigger_id inválido"}` (HTTP 200).

  2. `list_recent_tasks` only read Safent's SQL agent_tasks queue. Neus's own
     cron scheduler fires jobs without ever enqueueing into that table, so
     every cron-fired run was invisible there — `/tasks/recent` was always
     `[]` even right after the journal showed `triggers.timer.fired`.

Fix: `_neus_job_to_task_dict` prefers `origin.trigger_instance_id` (falls
back to the raw Neus id only for jobs with no origin — created directly via
the agent's own `cronjob` tool, which never went through the authorization
gate and so have no Safent UUID to report). `list_recent_tasks` merges in
synthesized rows from Neus jobs' own `last_run_at`/`last_status`.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_DBUS_MODULE = "hermes.agents_os.infrastructure.dbus_runtime_service"


def _ensure_cron_jobs_stub() -> None:
    """Same pattern as tests/unit/test_tasks_dashboard.py: seed a stub
    cron.jobs module (hermes-agent isn't installed in this checkout) so
    `from cron.jobs import list_jobs` inside the wiring resolves to
    something patchable."""
    if "cron" not in sys.modules:
        sys.modules["cron"] = types.ModuleType("cron")
    if "cron.jobs" not in sys.modules:
        mod = types.ModuleType("cron.jobs")
        mod.list_jobs = lambda include_disabled=True: []  # type: ignore[attr-defined]
        sys.modules["cron.jobs"] = mod


def _make_wiring(cp_service: Any = None):
    import os as _os

    from hermes.agents_os.infrastructure.dbus_runtime_service import (
        DbusRuntimeServiceWiring,
    )
    from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

    class _FakeApprovalGate:
        async def approve(self, *, proposal_id, approved_by) -> str:
            return f"token-{proposal_id}"
        async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...

    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_FakeApprovalGate(),
        authorized_uids=frozenset({_os.getuid()}),
        control_plane_service=cp_service,
    )


# A job created through Safent's create_scheduled_task — has origin.trigger_instance_id.
_SAFENT_TRIGGER_UUID = "3fae2f6a-8f1e-4b0e-9b1a-6c8a2c7e9d10"
_UI_CREATED_JOB = {
    "id": "c2217361b797",  # the Neus job's own short id — must NOT leak as trigger_id
    "name": "Daily report",
    "prompt": "Compile the daily report",
    "schedule": {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"},
    "schedule_display": "0 9 * * *",
    "enabled": True,
    "next_run_at": None,
    "last_run_at": "2026-09-10T12:41:39+00:00",
    "last_status": "completed",
    "repeat": None,
    "origin": {"trigger_instance_id": _SAFENT_TRIGGER_UUID, "source": "safent_scheduled_task"},
}

# A job the agent created directly (its own `cronjob` tool) — no origin at all.
_AGENT_CREATED_JOB = {
    "id": "aabb001122cc",
    "name": "Hourly check",
    "prompt": "Run hourly check",
    "schedule": {"kind": "cron", "expr": "0 * * * *", "display": "0 * * * *"},
    "schedule_display": "0 * * * *",
    "enabled": True,
    "next_run_at": None,
    "last_run_at": None,
    "last_status": None,
    "repeat": None,
}


class TestConfiguredTasksTriggerId:
    async def test_ui_created_job_reports_the_safent_uuid_not_the_neus_id(self) -> None:
        _ensure_cron_jobs_stub()
        wiring = _make_wiring(cp_service=None)

        with patch("cron.jobs.list_jobs", return_value=[_UI_CREATED_JOB]):
            result = await wiring.list_configured_tasks()

        assert len(result) == 1
        row = result[0]
        assert row["trigger_id"] == _SAFENT_TRIGGER_UUID
        assert row["trigger_id"] != _UI_CREATED_JOB["id"]

    async def test_agent_created_job_falls_back_to_the_neus_id(self) -> None:
        """No origin -> no Safent UUID exists at all; the raw Neus id is the
        only honest identifier (pre-existing, separate limitation — those
        rows were never authorized through the Safent gate)."""
        _ensure_cron_jobs_stub()
        wiring = _make_wiring(cp_service=None)

        with patch("cron.jobs.list_jobs", return_value=[_AGENT_CREATED_JOB]):
            result = await wiring.list_configured_tasks()

        assert result[0]["trigger_id"] == "aabb001122cc"

    async def test_returned_trigger_id_is_what_toggle_and_delete_actually_require(self) -> None:
        """End-to-end proof: the id the LIST hands back must parse as a UUID
        (what set_scheduled_task_enabled/delete_scheduled_task require) for a
        UI-created job — this is exactly what broke the CalendarView toggle
        with 'trigger_id inválido'."""
        from uuid import UUID

        _ensure_cron_jobs_stub()
        wiring = _make_wiring(cp_service=None)

        with patch("cron.jobs.list_jobs", return_value=[_UI_CREATED_JOB]):
            result = await wiring.list_configured_tasks()

        UUID(result[0]["trigger_id"])  # must not raise


class TestRecentTasksIncludesCronRuns:
    async def test_cron_run_with_last_run_at_appears_in_recent(self) -> None:
        _ensure_cron_jobs_stub()
        wiring = _make_wiring(cp_service=None)

        with patch("cron.jobs.list_jobs", return_value=[_UI_CREATED_JOB]):
            result = await wiring.list_recent_tasks()

        assert len(result) == 1
        row = result[0]
        assert row["task_id"] == _SAFENT_TRIGGER_UUID
        assert row["status"] == "completed"
        assert row["trigger_kind"] == "timer"
        assert row["enqueued_at"] == _UI_CREATED_JOB["last_run_at"]

    async def test_cron_job_never_fired_does_not_appear_in_recent(self) -> None:
        """last_run_at still null -> the job hasn't run; must not fabricate
        a phantom 'recent' entry for it."""
        _ensure_cron_jobs_stub()
        wiring = _make_wiring(cp_service=None)

        with patch("cron.jobs.list_jobs", return_value=[_AGENT_CREATED_JOB]):
            result = await wiring.list_recent_tasks()

        assert result == []

    async def test_recent_was_always_empty_before_the_fix(self) -> None:
        """Pin the exact symptom from the matrix: with only cp_service=None
        (no SQL rows — the common case, since cron never wrote there) and a
        fired Neus job, /tasks/recent must NOT be []."""
        _ensure_cron_jobs_stub()
        wiring = _make_wiring(cp_service=None)

        with patch("cron.jobs.list_jobs", return_value=[_UI_CREATED_JOB]):
            result = await wiring.list_recent_tasks()

        assert result != []
