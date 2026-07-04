"""security_hook approval-route CONSULT — Enterprise Fase 2 Phase 4a.

Covers the NEW Step 1.6a (_log_approval_route), wired between Step 1.5
(policy) and Step 1.6 (hook_mfa_block):

  - The consult NEVER changes what blocks/allows/cards today: with the
    tenant-remote-approval flag at its default (False), behaviour is
    byte-identical whether or not the consult runs (A/B against the same
    hook calls).
  - The route log line is emitted for every native tool call.
  - An ENTERPRISE-eligible action (MOST_DELICATE tool, cloud-managed agent)
    still takes the LOCAL path — the flag defaults False, so ENTERPRISE is
    never even reachable in this phase.
  - A failure inside the consult (e.g. a raising access-scope repo) is
    logged and swallowed — it must never turn an ALLOW into a BLOCK.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes.capabilities.domain.agent_access_scope import AgentAccessScope
from hermes.runtime.conversation_task_registry import (
    clear_current_cycle_agent,
    set_current_cycle_agent,
)
from hermes.runtime.security_hook import make_pre_tool_call_hook

pytestmark = pytest.mark.unit

_TENANT_ID = "tenant-x"
_NORMAL_TOOL = "read_file"  # NORMAL delicacy, native, never blocks on its own
_MOST_DELICATE_TOOL = "cronjob"  # MOST_DELICATE — Enterprise-eligible by delicacy


class _FakeAccessScopeRepo:
    def __init__(self, scope: AgentAccessScope | None) -> None:
        self._scope = scope

    def get_scope(self, agent_id: str, tenant_id: str) -> AgentAccessScope | None:
        return self._scope


class _RaisingAccessScopeRepo:
    def get_scope(self, agent_id: str, tenant_id: str) -> AgentAccessScope | None:
        raise RuntimeError("db unavailable")


def _make_hook(access_scope_repo=None, tenant_id: str = _TENANT_ID):
    agent_state = MagicMock()
    agent_state.is_paused = AsyncMock(return_value=False)
    loop = asyncio.new_event_loop()
    broker = MagicMock()
    broker._os_native_dispatcher = None  # skip denylist check

    return make_pre_tool_call_hook(
        agent_state=agent_state,
        engine_loop=loop,
        broker=broker,
        access_scope_repo=access_scope_repo,
        tenant_id=tenant_id,
    )


def _run_hook(hook, tool_name: str, args: dict | None = None):
    with patch("hermes.runtime.security_hook._check_kill_switch", return_value=False):
        return hook(tool_name=tool_name, args=args or {})


@pytest.fixture(autouse=True)
def _clean_ambient_agent():
    clear_current_cycle_agent()
    yield
    clear_current_cycle_agent()


# ---------------------------------------------------------------------------
# Byte-identical behaviour: the consult never changes the outcome
# ---------------------------------------------------------------------------


class TestConsultIsInert:
    def test_normal_tool_still_allowed(self) -> None:
        hook = _make_hook()
        assert _run_hook(hook, _NORMAL_TOOL, {"path": "/tmp/x.txt"}) is None

    def test_outcome_identical_with_and_without_the_consult_wired(self) -> None:
        """A/B: patch _log_approval_route to a no-op and confirm the result
        is unchanged — the consult contributes ZERO to the decision."""
        hook = _make_hook()
        baseline = _run_hook(hook, _NORMAL_TOOL, {"path": "/tmp/x.txt"})

        with patch("hermes.runtime.security_hook._log_approval_route", return_value=None):
            with_noop_consult = _run_hook(hook, _NORMAL_TOOL, {"path": "/tmp/x.txt"})

        assert baseline == with_noop_consult

    def test_enterprise_eligible_action_still_takes_local_path(self) -> None:
        """cronjob is MOST_DELICATE (Enterprise-eligible by delicacy) on an
        agent whose scope reports managed_by='cloud' — but
        _tenant_remote_approval_enabled() defaults False, so ENTERPRISE is
        never reachable: this must behave EXACTLY like today's MOST_DELICATE
        MFA-block path (hook_mfa_block), not some new remote-approval path."""
        set_current_cycle_agent("agent-a")
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id="agent-a", updated_by=1,
            managed_by="cloud",
        )
        hook = _make_hook(_FakeAccessScopeRepo(scope=scope))

        with patch(
            "hermes.runtime.security_hook._resolve_native_danger_approval",
            return_value="pending owner approval",
        ) as mock_native_danger:
            result = _run_hook(hook, _MOST_DELICATE_TOOL, {})

        # Reaches the LOCAL native-danger approval path (block-and-resume) —
        # exactly like today, never a remote/Enterprise branch.
        mock_native_danger.assert_called_once()
        assert result is not None
        assert result.get("action") == "block"


# ---------------------------------------------------------------------------
# Observability: the route is logged
# ---------------------------------------------------------------------------


class TestRouteIsLogged:
    def test_route_log_line_emitted_for_native_tool(self, caplog: pytest.LogCaptureFixture) -> None:
        hook = _make_hook()
        with caplog.at_level(logging.INFO, logger="hermes.runtime.security_hook"):
            _run_hook(hook, _NORMAL_TOOL, {"path": "/tmp/x.txt"})

        route_logs = [
            r for r in caplog.records
            if r.message.startswith("hermes.security_hook.approval_route route=")
        ]
        assert len(route_logs) == 1
        assert f"tool={_NORMAL_TOOL}" in route_logs[0].message

    def test_route_is_local_when_flag_defaults_false(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        set_current_cycle_agent("agent-a")
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id="agent-a", updated_by=1,
            managed_by="cloud",
        )
        hook = _make_hook(_FakeAccessScopeRepo(scope=scope))

        with (
            caplog.at_level(logging.INFO, logger="hermes.runtime.security_hook"),
            patch(
                "hermes.runtime.security_hook._resolve_native_danger_approval",
                return_value=None,
            ),
        ):
            _run_hook(hook, _NORMAL_TOOL, {"path": "/tmp/x.txt"})

        route_logs = [
            r for r in caplog.records
            if r.message.startswith("hermes.security_hook.approval_route route=")
        ]
        assert len(route_logs) == 1
        assert "route=local" in route_logs[0].message


# ---------------------------------------------------------------------------
# Fail-soft: a consult error never turns ALLOW into BLOCK
# ---------------------------------------------------------------------------


class TestConsultFailsSoft:
    def test_resolve_agent_managed_by_fails_soft_on_raising_repo(self) -> None:
        """Direct unit test of the new helper in isolation — a raising repo
        must degrade to None (=> never cloud-gated), never raise. Exercised
        directly because Steps 1.1/1.4 (pre-existing, untouched) ALSO call
        access_scope_repo.get_scope and already fail-closed on any repo error
        before Step 1.6a is ever reached — so a full-hook run cannot isolate
        THIS function's own fail-soft behaviour."""
        from hermes.runtime.security_hook import _resolve_agent_managed_by

        set_current_cycle_agent("agent-a")
        result = _resolve_agent_managed_by(_RaisingAccessScopeRepo(), _TENANT_ID)
        assert result is None

    def test_router_raising_is_logged_and_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        hook = _make_hook()

        with (
            caplog.at_level(logging.WARNING, logger="hermes.runtime.security_hook"),
            patch(
                "hermes.capabilities.approval_router.route",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = _run_hook(hook, _NORMAL_TOOL, {"path": "/tmp/x.txt"})

        assert result is None  # never blocks because of the consult
        failure_logs = [
            r for r in caplog.records
            if r.message.startswith(
                "hermes.security_hook.approval_route_consult_failed"
            )
        ]
        assert len(failure_logs) == 1
