"""Unit tests — R6: cron.jobs mutation propagation (update / delete / enable).

Invariants pinned:
  R6-1. _neus_cron_find_job_id_by_trigger returns the job id whose
        origin.trigger_instance_id matches the given trigger_id.
  R6-2. _neus_cron_find_job_id_by_trigger returns None when no job matches.
  R6-3. _neus_cron_find_job_id_by_trigger returns None when cron.jobs is absent.
  R6-4. _neus_cron_update_job calls update_job with the non-None fields.
  R6-5. _neus_cron_update_job returns False (no raise) when job not found.
  R6-6. _neus_cron_update_job returns False (no raise) when cron.jobs absent.
  R6-7. _neus_cron_update_job returns False (no raise) on update_job exception.
  R6-8. _neus_cron_remove_job calls remove_job with the resolved id.
  R6-9. _neus_cron_remove_job returns False (no raise) when job not found.
  R6-10. _neus_cron_remove_job returns False (no raise) when cron.jobs absent.
  R6-11. _neus_cron_remove_job returns False (no raise) on remove_job exception.
  R6-12. _neus_cron_set_enabled calls resume_job when enabled=True.
  R6-13. _neus_cron_set_enabled calls pause_job when enabled=False.
  R6-14. _neus_cron_set_enabled returns False (no raise) when job not found.
  R6-15. _neus_cron_set_enabled returns False (no raise) when cron.jobs absent.
  R6-16. _neus_cron_create_job passes origin kwarg to create_job when provided.
  R6-17. create_scheduled_task passes origin with trigger_instance_id to cron create.
  R6-18. delete_scheduled_task calls _neus_cron_remove_job; trigger_repo.revoke ok.
  R6-19. set_scheduled_task_enabled calls _neus_cron_set_enabled; trigger ok.
  R6-20. update_scheduled_task calls _neus_cron_update_job; trigger_repo.update ok.
  R6-21. delete_scheduled_task succeeds (ok=True) even when cron.jobs absent.
  R6-22. set_scheduled_task_enabled succeeds (ok=True) even when cron.jobs absent.
  R6-23. update_scheduled_task returns updated task even when cron.jobs absent.
  R6-24. _neus_cron_remove_job_soft (timer one-shot) calls remove_job on match.
  R6-25. _neus_cron_remove_job_soft is fail-soft when cron.jobs absent.
  R6-26. _neus_cron_remove_job_soft is fail-soft when trigger not found in catalog.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Stub cron.jobs when not installed
# ---------------------------------------------------------------------------


def _ensure_cron_stubs() -> None:
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


_ensure_cron_stubs()

from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: E402
    _neus_cron_create_job,
    _neus_cron_find_job_id_by_trigger,
    _neus_cron_remove_job,
    _neus_cron_set_enabled,
    _neus_cron_update_job,
)
from hermes.tasks.triggers.application.timer_trigger_source import (  # noqa: E402
    _neus_cron_remove_job_soft,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(job_id: str, trigger_id: str, **overrides: Any) -> dict:
    base: dict = {
        "id": job_id,
        "name": "Test job",
        "prompt": "do stuff",
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "enabled": True,
        "origin": {
            "trigger_instance_id": trigger_id,
            "source": "safent_scheduled_task",
        },
    }
    base.update(overrides)
    return base


def _make_wiring() -> Any:
    from hermes.agents_os.infrastructure.dbus_runtime_service import (
        DbusRuntimeServiceWiring,
    )
    return DbusRuntimeServiceWiring(
        agent_state=None,
        approval_gate=None,
        authorized_uids=frozenset({1000}),
        work_queue=None,
        wake_signal=None,
    )


# ===========================================================================
# R6-1 … R6-3: _neus_cron_find_job_id_by_trigger
# ===========================================================================


class TestNeusCronFindJobIdByTrigger:
    def test_r6_1_returns_job_id_on_match(self):
        jobs = [_make_job("job-abc", "trigger-123")]
        with patch("cron.jobs.list_jobs", return_value=jobs):
            result = _neus_cron_find_job_id_by_trigger("trigger-123")
        assert result == "job-abc"

    def test_r6_2_returns_none_when_no_match(self):
        jobs = [_make_job("job-abc", "trigger-999")]
        with patch("cron.jobs.list_jobs", return_value=jobs):
            result = _neus_cron_find_job_id_by_trigger("trigger-123")
        assert result is None

    def test_r6_2b_returns_none_on_empty_catalog(self):
        with patch("cron.jobs.list_jobs", return_value=[]):
            result = _neus_cron_find_job_id_by_trigger("trigger-123")
        assert result is None

    def test_r6_3_returns_none_when_cron_unavailable(self):
        original = sys.modules.get("cron.jobs")
        try:
            sys.modules["cron.jobs"] = None  # type: ignore[assignment]
            result = _neus_cron_find_job_id_by_trigger("trigger-123")
        finally:
            if original is not None:
                sys.modules["cron.jobs"] = original
        assert result is None

    def test_skips_jobs_with_no_origin(self):
        jobs = [{"id": "no-origin", "name": "x", "origin": None}]
        with patch("cron.jobs.list_jobs", return_value=jobs):
            result = _neus_cron_find_job_id_by_trigger("trigger-123")
        assert result is None

    def test_skips_jobs_with_wrong_source(self):
        jobs = [
            {
                "id": "other-tool",
                "name": "x",
                "origin": {"trigger_instance_id": "trigger-123", "source": "agent"},
            }
        ]
        # source is ignored; we only key on trigger_instance_id
        with patch("cron.jobs.list_jobs", return_value=jobs):
            result = _neus_cron_find_job_id_by_trigger("trigger-123")
        assert result == "other-tool"


# ===========================================================================
# R6-4 … R6-7: _neus_cron_update_job
# ===========================================================================


class TestNeusCronUpdateJob:
    def test_r6_4_calls_update_with_non_none_fields(self):
        jobs = [_make_job("job-xyz", "tid-1")]
        called: list[tuple] = []

        def fake_update(job_id: str, updates: dict) -> None:
            called.append((job_id, updates))

        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.update_job", side_effect=fake_update):
            result = _neus_cron_update_job(
                "tid-1", prompt="new prompt", schedule="0 10 * * *", name="New name"
            )

        assert result is True
        assert len(called) == 1
        job_id, updates = called[0]
        assert job_id == "job-xyz"
        assert updates == {"prompt": "new prompt", "schedule": "0 10 * * *", "name": "New name"}

    def test_r6_4_excludes_none_fields(self):
        jobs = [_make_job("job-xyz", "tid-1")]
        called: list[tuple] = []

        def fake_update(job_id: str, updates: dict) -> None:
            called.append((job_id, updates))

        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.update_job", side_effect=fake_update):
            _neus_cron_update_job("tid-1", prompt="only prompt")

        assert called[0][1] == {"prompt": "only prompt"}

    def test_r6_4_no_op_when_all_fields_none(self):
        jobs = [_make_job("job-xyz", "tid-1")]
        called: list = []
        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.update_job", side_effect=lambda *a, **kw: called.append(a)):
            result = _neus_cron_update_job("tid-1")
        assert result is True
        assert len(called) == 0  # no-op when nothing to update

    def test_r6_5_returns_false_when_job_not_found(self):
        with patch("cron.jobs.list_jobs", return_value=[]):
            result = _neus_cron_update_job("tid-missing", prompt="x")
        assert result is False

    def test_r6_6_returns_false_when_cron_unavailable(self):
        original = sys.modules.get("cron.jobs")
        try:
            sys.modules["cron.jobs"] = None  # type: ignore[assignment]
            result = _neus_cron_update_job("tid-1", prompt="x")
        finally:
            if original is not None:
                sys.modules["cron.jobs"] = original
        assert result is False

    def test_r6_7_returns_false_on_update_job_exception(self):
        jobs = [_make_job("job-xyz", "tid-1")]
        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.update_job", side_effect=RuntimeError("db locked")):
            result = _neus_cron_update_job("tid-1", prompt="x")
        assert result is False


# ===========================================================================
# R6-8 … R6-11: _neus_cron_remove_job
# ===========================================================================


class TestNeusCronRemoveJob:
    def test_r6_8_calls_remove_job_with_correct_id(self):
        jobs = [_make_job("job-del", "tid-del")]
        removed: list = []
        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.remove_job", side_effect=removed.append):
            result = _neus_cron_remove_job("tid-del")
        assert result is True
        assert removed == ["job-del"]

    def test_r6_9_returns_false_when_job_not_found(self):
        with patch("cron.jobs.list_jobs", return_value=[]):
            result = _neus_cron_remove_job("tid-missing")
        assert result is False

    def test_r6_10_returns_false_when_cron_unavailable(self):
        original = sys.modules.get("cron.jobs")
        try:
            sys.modules["cron.jobs"] = None  # type: ignore[assignment]
            result = _neus_cron_remove_job("tid-1")
        finally:
            if original is not None:
                sys.modules["cron.jobs"] = original
        assert result is False

    def test_r6_11_returns_false_on_remove_job_exception(self):
        jobs = [_make_job("job-xyz", "tid-1")]
        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.remove_job", side_effect=OSError("permission denied")):
            result = _neus_cron_remove_job("tid-1")
        assert result is False


# ===========================================================================
# R6-12 … R6-15: _neus_cron_set_enabled
# ===========================================================================


class TestNeusCronSetEnabled:
    def test_r6_12_calls_resume_job_when_enabled_true(self):
        jobs = [_make_job("job-toggle", "tid-toggle")]
        resumed: list = []
        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.resume_job", side_effect=resumed.append):
            result = _neus_cron_set_enabled("tid-toggle", enabled=True)
        assert result is True
        assert resumed == ["job-toggle"]

    def test_r6_13_calls_pause_job_when_enabled_false(self):
        jobs = [_make_job("job-toggle", "tid-toggle")]
        paused: list = []

        def fake_pause(job_id: str, reason: str = "") -> None:
            paused.append((job_id, reason))

        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.pause_job", side_effect=fake_pause):
            result = _neus_cron_set_enabled("tid-toggle", enabled=False)
        assert result is True
        assert paused[0][0] == "job-toggle"

    def test_r6_14_returns_false_when_job_not_found(self):
        with patch("cron.jobs.list_jobs", return_value=[]):
            result = _neus_cron_set_enabled("tid-missing", enabled=True)
        assert result is False

    def test_r6_15_returns_false_when_cron_unavailable(self):
        original = sys.modules.get("cron.jobs")
        try:
            sys.modules["cron.jobs"] = None  # type: ignore[assignment]
            result = _neus_cron_set_enabled("tid-1", enabled=True)
        finally:
            if original is not None:
                sys.modules["cron.jobs"] = original
        assert result is False

    def test_returns_false_on_resume_exception(self):
        jobs = [_make_job("job-toggle", "tid-toggle")]
        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.resume_job", side_effect=RuntimeError("timeout")):
            result = _neus_cron_set_enabled("tid-toggle", enabled=True)
        assert result is False


# ===========================================================================
# R6-16: _neus_cron_create_job passes origin
# ===========================================================================


class TestNeusCronCreateJobOrigin:
    def test_r6_16_passes_origin_to_create_job(self):
        created: list = []

        def fake_create(**kw: Any) -> dict:
            created.append(kw)
            return {"id": "new-id"}

        origin = {"trigger_instance_id": "uuid-abc", "source": "safent_scheduled_task"}
        with patch("cron.jobs.create_job", side_effect=fake_create):
            _neus_cron_create_job(
                prompt="p", schedule="0 9 * * *", name="n", one_shot=False, origin=origin
            )
        assert created[0]["origin"] == origin

    def test_origin_none_does_not_pass_origin_kwarg(self):
        created: list = []

        def fake_create(**kw: Any) -> dict:
            created.append(kw)
            return {"id": "new-id"}

        with patch("cron.jobs.create_job", side_effect=fake_create):
            _neus_cron_create_job(
                prompt="p", schedule="0 9 * * *", name="n", one_shot=False
            )
        assert "origin" not in created[0]


# ===========================================================================
# R6-17 … R6-23: wiring integration (DbusRuntimeServiceWiring)
# ===========================================================================


class TestWiringMutationPropagation:
    """Verify each mutating handler propagates to cron.jobs after trigger_repo."""

    def _make_trigger_repo_mock(
        self, trigger_instance_id: str = "aaaabbbb-cccc-dddd-eeee-111122223333"
    ) -> MagicMock:
        repo = MagicMock()
        trigger = MagicMock()
        trigger.trigger_instance_id = trigger_instance_id
        repo.authorize = AsyncMock(return_value=trigger)
        repo.revoke = AsyncMock()
        repo.update_task = MagicMock(return_value=True)
        return repo

    def _make_wiring_with_repo(self, repo: MagicMock) -> Any:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            DbusRuntimeServiceWiring,
        )
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({1000}),
            work_queue=None,
            wake_signal=None,
        )
        wiring._trigger_repo = repo
        return wiring

    @pytest.mark.asyncio
    async def test_r6_17_create_passes_origin_to_cron(self):
        """create_scheduled_task passes trigger_instance_id as origin to cron.jobs."""
        import json

        tid = "aaaabbbb-cccc-dddd-eeee-111122223333"
        repo = self._make_trigger_repo_mock(tid)
        wiring = self._make_wiring_with_repo(repo)

        created: list = []

        def fake_create(**kw: Any) -> dict:
            created.append(kw)
            return {"id": "new-cron-id"}

        draft = json.dumps({
            "title": "Morning report",
            "task_instruction": "Send the report",
            "cron": "0 9 * * 1-5",
            "one_shot": False,
            "risk_ceiling": "low",
        })

        with patch("cron.jobs.create_job", side_effect=fake_create), \
             patch(
                 "hermes.agents_os.infrastructure.dbus_runtime_service._patch_trigger_p3_fields",
                 new=AsyncMock(),
             ):
            result = await wiring.create_scheduled_task(draft_json=draft, sender_uid=1000)

        assert result["ok"] is True
        assert len(created) == 1
        assert created[0]["origin"]["trigger_instance_id"] == tid
        assert created[0]["origin"]["source"] == "safent_scheduled_task"

    @pytest.mark.asyncio
    async def test_r6_18_delete_calls_neus_remove_after_revoke(self):
        """delete_scheduled_task revokes trigger_repo AND removes from cron.jobs."""
        import uuid

        tid = str(uuid.uuid4())
        repo = self._make_trigger_repo_mock()
        wiring = self._make_wiring_with_repo(repo)

        jobs = [_make_job("job-to-del", tid)]
        removed: list = []

        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.remove_job", side_effect=removed.append):
            result = await wiring.delete_scheduled_task(trigger_id=tid, sender_uid=1000)

        assert result["ok"] is True
        repo.revoke.assert_awaited_once()
        assert removed == ["job-to-del"]

    @pytest.mark.asyncio
    async def test_r6_21_delete_ok_even_when_cron_unavailable(self):
        """delete_scheduled_task returns ok=True even when cron.jobs absent."""
        import uuid

        tid = str(uuid.uuid4())
        repo = self._make_trigger_repo_mock()
        wiring = self._make_wiring_with_repo(repo)

        original = sys.modules.get("cron.jobs")
        try:
            sys.modules["cron.jobs"] = None  # type: ignore[assignment]
            result = await wiring.delete_scheduled_task(trigger_id=tid, sender_uid=1000)
        finally:
            if original is not None:
                sys.modules["cron.jobs"] = original

        assert result["ok"] is True
        repo.revoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_r6_19_set_enabled_calls_neus_set_enabled(self):
        """set_scheduled_task_enabled propagates pause/resume to cron.jobs."""
        import uuid

        tid = str(uuid.uuid4())
        repo = self._make_trigger_repo_mock()
        wiring = self._make_wiring_with_repo(repo)

        jobs = [_make_job("job-toggle", tid)]
        resumed: list = []

        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.resume_job", side_effect=resumed.append), \
             patch(
                 "hermes.agents_os.infrastructure.dbus_runtime_service._set_trigger_enabled",
                 new=AsyncMock(),
             ):
            result = await wiring.set_scheduled_task_enabled(
                trigger_id=tid, enabled=True, sender_uid=1000
            )

        assert result["ok"] is True
        assert resumed == ["job-toggle"]

    @pytest.mark.asyncio
    async def test_r6_22_set_enabled_ok_even_when_cron_unavailable(self):
        """set_scheduled_task_enabled returns ok=True even when cron.jobs absent."""
        import uuid

        tid = str(uuid.uuid4())
        repo = self._make_trigger_repo_mock()
        wiring = self._make_wiring_with_repo(repo)

        original = sys.modules.get("cron.jobs")
        try:
            sys.modules["cron.jobs"] = None  # type: ignore[assignment]
            with patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service._set_trigger_enabled",
                new=AsyncMock(),
            ):
                result = await wiring.set_scheduled_task_enabled(
                    trigger_id=tid, enabled=False, sender_uid=1000
                )
        finally:
            if original is not None:
                sys.modules["cron.jobs"] = original

        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_r6_20_update_calls_neus_update_job(self):
        """update_scheduled_task propagates new fields to cron.jobs."""
        import json
        import uuid

        tid = str(uuid.uuid4())
        repo = self._make_trigger_repo_mock()
        wiring = self._make_wiring_with_repo(repo)

        jobs = [_make_job("job-upd", tid)]
        updated: list[tuple] = []

        def fake_update_job(job_id: str, updates: dict) -> None:
            updated.append((job_id, updates))

        draft = json.dumps({
            "label": "New label",
            "instruction": "Do the new thing",
            "cron": "0 10 * * *",
        })

        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.update_job", side_effect=fake_update_job), \
             patch.object(wiring, "get_scheduled_task", new=AsyncMock(return_value={"ok": True})):
            result = await wiring.update_scheduled_task(
                trigger_id=tid, draft_json=draft, sender_uid=1000
            )

        assert repo.update_task.called
        assert len(updated) == 1
        job_id, changes = updated[0]
        assert job_id == "job-upd"
        assert changes["prompt"] == "Do the new thing"
        assert changes["schedule"] == "0 10 * * *"
        assert changes["name"] == "New label"

    @pytest.mark.asyncio
    async def test_r6_23_update_returns_task_even_when_cron_unavailable(self):
        """update_scheduled_task returns updated dict even when cron.jobs absent."""
        import json
        import uuid

        tid = str(uuid.uuid4())
        repo = self._make_trigger_repo_mock()
        wiring = self._make_wiring_with_repo(repo)

        draft = json.dumps({
            "label": "Label",
            "instruction": "Do stuff",
            "cron": "0 9 * * *",
        })

        original = sys.modules.get("cron.jobs")
        try:
            sys.modules["cron.jobs"] = None  # type: ignore[assignment]
            with patch.object(
                wiring,
                "get_scheduled_task",
                new=AsyncMock(return_value={"trigger_id": tid, "label": "Label"}),
            ):
                result = await wiring.update_scheduled_task(
                    trigger_id=tid, draft_json=draft, sender_uid=1000
                )
        finally:
            if original is not None:
                sys.modules["cron.jobs"] = original

        assert result["trigger_id"] == tid
        repo.update_task.assert_called_once()


# ===========================================================================
# R6-24 … R6-26: _neus_cron_remove_job_soft (one-shot timer cleanup)
# ===========================================================================


class TestNeusCronRemoveJobSoft:
    def test_r6_24_calls_remove_job_on_match(self):
        jobs = [_make_job("one-shot-job", "os-trigger-1")]
        removed: list = []

        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.remove_job", side_effect=removed.append):
            _neus_cron_remove_job_soft("os-trigger-1")

        assert removed == ["one-shot-job"]

    def test_r6_25_fail_soft_when_cron_unavailable(self):
        original = sys.modules.get("cron.jobs")
        try:
            sys.modules["cron.jobs"] = None  # type: ignore[assignment]
            # Must not raise
            _neus_cron_remove_job_soft("os-trigger-1")
        finally:
            if original is not None:
                sys.modules["cron.jobs"] = original

    def test_r6_26_fail_soft_when_trigger_not_in_catalog(self):
        with patch("cron.jobs.list_jobs", return_value=[]), \
             patch("cron.jobs.remove_job") as mock_remove:
            _neus_cron_remove_job_soft("os-trigger-missing")
        mock_remove.assert_not_called()

    def test_empty_trigger_id_is_no_op(self):
        with patch("cron.jobs.list_jobs") as mock_list:
            _neus_cron_remove_job_soft("")
        mock_list.assert_not_called()

    def test_fail_soft_on_remove_exception(self):
        jobs = [_make_job("one-shot-job", "os-trigger-1")]
        with patch("cron.jobs.list_jobs", return_value=jobs), \
             patch("cron.jobs.remove_job", side_effect=RuntimeError("crash")):
            # Must not raise
            _neus_cron_remove_job_soft("os-trigger-1")
