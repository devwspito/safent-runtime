"""Regression (matriz 10-sep, hallazgo B): "Programadas" / /tasks/recent
showed cron/jobs.json's last_run_at/last_status stuck at null and
next_run_at frozen, even after several confirmed `hermes.triggers.timer.fired`
journal entries and 6 timer/failed rows in agent_tasks.

Root cause: SchedulerTimerSource fires Safent-authorized timer triggers
directly through TriggerGate.enqueue_from_trigger (writes to agent_tasks) —
it NEVER touched Neus's cron/jobs.json for these rows (only
_neus_cron_remove_job_soft did, and only for one-shot revocation). Neus's
OWN scheduler loop — the thing that actually calls cron.jobs.mark_job_run —
never fires Safent-authorized jobs at all, so nothing ever wrote
last_run_at/last_status/next_run_at for them; they were born null/frozen and
stayed that way through every fire, success or failure.

Fix: every SchedulerTimerSource poll (_tick) now mirrors each timer
trigger's own last_run_at/last_status (native: agent_tasks, via
list_triggers_with_last_run) plus a freshly recomputed next_run_at into the
matching Neus job (_sync_neus_bookkeeping / _neus_cron_mark_run) — so a
LATER poll picks up whatever terminal status a worker eventually gave the
fired task, and next_run_at is never a stale one-time snapshot.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from hermes.tasks.triggers.application.timer_trigger_source import SchedulerTimerSource
from hermes.tasks.triggers.domain.authorized_trigger_ports import AuthorizedTriggerType, RiskCeiling
from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (
    SqliteAuthorizedTriggerRepository,
)

pytestmark = pytest.mark.unit

_ADMIN_UUID = uuid4()


def _ensure_cron_jobs_stub() -> types.ModuleType:
    """Same guarded pattern as test_neus_cron_mutation_propagation.py /
    test_cron_trigger_id_and_recent.py: create the stub ONLY if `cron.jobs`
    isn't already registered, and never replace an existing module — other
    test files' stubs (with create_job/update_job/remove_job/...) live in
    the SAME process-wide sys.modules and must not be clobbered."""
    if "cron" not in sys.modules:
        sys.modules["cron"] = types.ModuleType("cron")
    if "cron.jobs" not in sys.modules:
        mod = types.ModuleType("cron.jobs")
        mod.list_jobs = lambda include_disabled=True: []  # type: ignore[attr-defined]
        mod.create_job = lambda **kw: {"id": "stub001"}  # type: ignore[attr-defined]
        mod.update_job = lambda job_id, updates: None  # type: ignore[attr-defined]
        mod.remove_job = lambda job_id: None  # type: ignore[attr-defined]
        mod.pause_job = lambda job_id, reason="": None  # type: ignore[attr-defined]
        mod.resume_job = lambda job_id: None  # type: ignore[attr-defined]
        sys.modules["cron.jobs"] = mod
    return sys.modules["cron.jobs"]


async def _seed_timer_trigger(repo: SqliteAuthorizedTriggerRepository, *, scope: str):
    trigger = await repo.authorize(
        trigger_type=AuthorizedTriggerType.TIMER,
        scope_value=scope,
        allowed_capabilities=("list_services",),
        risk_ceiling=RiskCeiling.LOW,
        admin_uuid=_ADMIN_UUID,
        approval_signature="test-sig",
    )
    return trigger.trigger_instance_id


def _insert_agent_task(repo: SqliteAuthorizedTriggerRepository, *, trigger_instance_id: str, status: str) -> str:
    import json

    task_id = str(uuid4())
    now = datetime.now(tz=UTC).isoformat()
    admin_str = str(_ADMIN_UUID)
    payload = json.dumps({"enqueued_by": admin_str, "instruction": "scheduled run"})
    repo._conn.execute(  # noqa: SLF001
        """
        INSERT INTO agent_tasks (
            task_id, trigger_kind, enqueued_by, operator_id,
            instruction, payload_json, status,
            kind, priority, retry_count, max_retries,
            created_at, updated_at,
            trigger_instance_id, tenant_id, worker_id
        ) VALUES (?, 'timer', ?, ?, ?, ?, ?, 'autonomous', 0, 1, 3, ?, ?, ?, ?, ?)
        """,
        (
            task_id, admin_str, admin_str, "scheduled run", payload, status,
            now, now, trigger_instance_id, str(uuid4()), "worker-0",
        ),
    )
    repo._conn.commit()  # noqa: SLF001
    return task_id


class _FakeGate:
    """Mimics TriggerGate.enqueue_from_trigger by writing a real agent_tasks
    row on the SAME repo connection — so the NEXT poll's
    list_triggers_with_last_run() sees exactly what a real fire would leave
    behind, without wiring a full SqliteWorkQueue for this test."""

    def __init__(self, repo: SqliteAuthorizedTriggerRepository, trigger_id: str) -> None:
        self._repo = repo
        self._trigger_id = trigger_id
        self.fire_count = 0

    async def enqueue_from_trigger(self, **_kwargs: object) -> str:
        self.fire_count += 1
        return _insert_agent_task(self._repo, trigger_instance_id=self._trigger_id, status="pending")


class TestSchedulerTimerSourceSyncsNeusBookkeeping:
    async def test_fake_scheduler_firing_twice_keeps_jobs_json_current(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cron_mod = _ensure_cron_jobs_stub()
        repo = SqliteAuthorizedTriggerRepository.in_memory()
        trigger_id = await _seed_timer_trigger(repo, scope="0 0 1 1 *")  # once a year — never "due" mid-test
        gate = _FakeGate(repo, str(trigger_id))
        source = SchedulerTimerSource(gate=gate, repo=repo, poll_interval_s=0.01)

        job = {
            "id": "neus-job-1",
            "name": "yearly report",
            "origin": {"trigger_instance_id": str(trigger_id)},
        }
        monkeypatch.setattr(cron_mod, "list_jobs", lambda include_disabled=True: [job])
        update_calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            cron_mod, "update_job",
            lambda job_id, updates: update_calls.append((job_id, updates)),
        )

        # Poll 1: trigger never fired (last_run_at is None) — nothing to
        # mirror yet, honest-empty (matches _neus_cron_recent_runs' contract).
        await source._tick()  # noqa: SLF001
        assert update_calls == []

        # Simulate a fire having happened (as SchedulerTimerSource._fire
        # would via the real gate) — insert the agent_tasks row directly so
        # the trigger now has a real last_run_at/last_status to mirror.
        _insert_agent_task(repo, trigger_instance_id=str(trigger_id), status="failed")

        # Poll 2 ("fires" a second time, in spirit — the fake scheduler
        # re-polls and must now push the resolved outcome).
        await source._tick()  # noqa: SLF001

        assert len(update_calls) == 1
        job_id, updates = update_calls[0]
        assert job_id == "neus-job-1"
        assert updates["last_status"] == "failed"
        assert updates["last_run_at"]  # non-empty — no longer null
        assert updates["next_run_at"]  # non-empty — no longer frozen/empty

        # A third poll with a LATER resolved status proves this is a live
        # mirror, not a one-time write — "success or failure" both surface.
        latest = repo._conn.execute(  # noqa: SLF001
            "SELECT task_id FROM agent_tasks WHERE trigger_instance_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (str(trigger_id),),
        ).fetchone()
        repo._conn.execute(  # noqa: SLF001
            "UPDATE agent_tasks SET status = 'failed' WHERE task_id = ?", (latest[0],),
        )
        repo._conn.commit()  # noqa: SLF001

        await source._tick()  # noqa: SLF001
        assert len(update_calls) == 2
        assert update_calls[1][1]["last_status"] == "failed"

    async def test_trigger_that_never_fired_is_never_synced(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cron_mod = _ensure_cron_jobs_stub()
        repo = SqliteAuthorizedTriggerRepository.in_memory()
        trigger_id = await _seed_timer_trigger(repo, scope="0 0 1 1 *")
        gate = AsyncMock()
        source = SchedulerTimerSource(gate=gate, repo=repo)

        job = {"id": "neus-job-2", "origin": {"trigger_instance_id": str(trigger_id)}}
        monkeypatch.setattr(cron_mod, "list_jobs", lambda include_disabled=True: [job])
        update_calls: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            cron_mod, "update_job",
            lambda job_id, updates: update_calls.append((job_id, updates)),
        )

        await source._tick()  # noqa: SLF001

        assert update_calls == []
