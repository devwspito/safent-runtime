"""security_hook Enterprise approval routing — Fase 2 Phase 4c (TOTP-keyed).

Supersedes Phase 4b's delicacy/sensitivity/irreversible eligibility calculus.
The consult happens INSIDE the native-danger gate (Step 1.6) — it is computed
ONLY for an action that ALREADY needs owner approval (hook_mfa_block fired),
and its result decides WHO resolves that SAME approval:

  - flag OFF (default) → LOCAL, byte-identical to today's native-danger path.
  - flag ON + the tool is MFA-tier (`tool_delicacy.is_mfa_required`) + the
    agent is cloud-managed → ENTERPRISE: `_resolve_native_danger_approval` is
    called with route=ApprovalRoute.ENTERPRISE (persists route='enterprise' +
    sensitivity + agent_id on the pending row — verified in
    test_hitl_enterprise_route_block_and_resume.py's end-to-end coverage).
    The worker has no TOTP, so they can never approve these, only deny.
  - A SIMPLE-tier tool (no TOTP, e.g. cronjob — MOST_DELICATE by delicacy()
    but explicitly carved out of the MFA tier) stays LOCAL even fully
    tenant-gated: the worker approves it alone with a plain click.
  - A tool that never reaches the native-danger gate (NORMAL delicacy, no MFA
    required) never triggers the routing consult at all.
  - A raising router fails SOFT to LOCAL — the existing, proven native-danger
    gate keeps blocking exactly as before; a bug in the NEW routing consult
    can never widen who approves nor skip the gate (I-3).
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes.capabilities.approval_router import ApprovalRoute
from hermes.capabilities.domain.agent_access_scope import AgentAccessScope
from hermes.runtime.conversation_task_registry import (
    clear_current_cycle_agent,
    set_current_cycle_agent,
)
from hermes.runtime.security_hook import make_pre_tool_call_hook

pytestmark = pytest.mark.unit

_TENANT_ID = "tenant-x"
_NORMAL_TOOL = "read_file"  # NORMAL delicacy, native, never needs owner MFA
# MOST_DELICATE by delicacy() (blocks unconditionally at hook_mfa_block) but
# explicitly carved OUT of the MFA tier (owner decision 2026-06-25) — a plain
# click suffices, so it must NEVER escalate to ENTERPRISE regardless of the
# tenant gate. Distinguishes the delicacy() axis from is_mfa_required().
_MOST_DELICATE_TOOL = "cronjob"
_MFA_TIER_TOOL = "skill_manage"  # MOST_DELICATE AND MFA-tier — the real Enterprise-eligible case


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


def _cloud_scope() -> AgentAccessScope:
    return AgentAccessScope.create(
        tenant_id=_TENANT_ID, agent_id="agent-a", updated_by=1, managed_by="cloud",
    )


@pytest.fixture(autouse=True)
def _clean_ambient_agent():
    clear_current_cycle_agent()
    yield
    clear_current_cycle_agent()


# ---------------------------------------------------------------------------
# The routing consult only fires at the native-danger gate
# ---------------------------------------------------------------------------


class TestRoutingOnlyAtDangerGate:
    def test_normal_tool_still_allowed_and_never_routed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A NORMAL-delicacy tool never needs owner MFA, so the routing consult
        is never even invoked for it — no danger_route log line, no block."""
        hook = _make_hook()
        with caplog.at_level(logging.INFO, logger="hermes.runtime.security_hook"):
            result = _run_hook(hook, _NORMAL_TOOL, {"path": "/tmp/x.txt"})

        assert result is None
        route_logs = [
            r for r in caplog.records
            if r.message.startswith("hermes.security_hook.pre.danger_route")
        ]
        assert route_logs == []


# ---------------------------------------------------------------------------
# flag OFF (default) — LOCAL, byte-identical to Phase 4a's native-danger path
# ---------------------------------------------------------------------------


class TestFlagOffStaysLocal:
    def test_enterprise_eligible_action_still_takes_local_path(self) -> None:
        """cronjob is MOST_DELICATE (Enterprise-eligible by delicacy) on an
        agent whose scope reports managed_by='cloud' — but
        _tenant_remote_approval_enabled() defaults False (unpaired/no license
        flag), so ENTERPRISE is never reachable: this must behave EXACTLY like
        today's MOST_DELICATE MFA-block path (hook_mfa_block), not some new
        remote-approval path."""
        set_current_cycle_agent("agent-a")
        hook = _make_hook(_FakeAccessScopeRepo(scope=_cloud_scope()))

        with patch(
            "hermes.runtime.security_hook._resolve_native_danger_approval",
            return_value="pending owner approval",
        ) as mock_native_danger:
            result = _run_hook(hook, _MOST_DELICATE_TOOL, {})

        mock_native_danger.assert_called_once()
        assert mock_native_danger.call_args.kwargs["route"] is ApprovalRoute.LOCAL
        assert result is not None
        assert result.get("action") == "block"

    def test_route_is_local_when_flag_defaults_false(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        set_current_cycle_agent("agent-a")
        hook = _make_hook(_FakeAccessScopeRepo(scope=_cloud_scope()))

        with (
            caplog.at_level(logging.INFO, logger="hermes.runtime.security_hook"),
            patch(
                "hermes.runtime.security_hook._resolve_native_danger_approval",
                return_value=None,
            ),
        ):
            _run_hook(hook, _MOST_DELICATE_TOOL, {})

        route_logs = [
            r for r in caplog.records
            if r.message.startswith("hermes.security_hook.pre.danger_route")
        ]
        assert len(route_logs) == 1
        assert "route=local" in route_logs[0].message


# ---------------------------------------------------------------------------
# flag ON + eligible + cloud-managed — ENTERPRISE
# ---------------------------------------------------------------------------


class TestFlagOnRoutesEnterprise:
    def test_mfa_tier_cloud_managed_action_routes_enterprise(self) -> None:
        set_current_cycle_agent("agent-a")
        hook = _make_hook(_FakeAccessScopeRepo(scope=_cloud_scope()))

        with (
            patch(
                "hermes.runtime.security_hook._tenant_remote_approval_enabled",
                return_value=True,
            ),
            patch(
                "hermes.runtime.security_hook._resolve_native_danger_approval",
                return_value="pending Enterprise approval",
            ) as mock_native_danger,
        ):
            result = _run_hook(hook, _MFA_TIER_TOOL, {})

        mock_native_danger.assert_called_once()
        assert mock_native_danger.call_args.kwargs["route"] is ApprovalRoute.ENTERPRISE
        assert mock_native_danger.call_args.kwargs["agent_id"] == "agent-a"
        # STILL blocks on the SAME local Event path — routing never bypasses HITL.
        assert result is not None
        assert result.get("action") == "block"

    def test_most_delicate_but_simple_mfa_tier_tool_stays_local_even_with_flag_on(
        self,
    ) -> None:
        """cronjob is MOST_DELICATE by delicacy() (hook_mfa_block ALWAYS fires)
        but is explicitly carved out of the MFA tier (is_mfa_required ==
        False) — routing must follow is_mfa_required, not the coarser
        delicacy() axis, even with the tenant fully gated. The worker
        approves it alone with a plain click; Enterprise is never involved."""
        set_current_cycle_agent("agent-a")
        hook = _make_hook(_FakeAccessScopeRepo(scope=_cloud_scope()))

        with (
            patch(
                "hermes.runtime.security_hook._tenant_remote_approval_enabled",
                return_value=True,
            ),
            patch(
                "hermes.runtime.security_hook._resolve_native_danger_approval",
                return_value="pending owner approval",
            ) as mock_native_danger,
        ):
            _run_hook(hook, _MOST_DELICATE_TOOL, {})

        mock_native_danger.assert_called_once()
        assert mock_native_danger.call_args.kwargs["route"] is ApprovalRoute.LOCAL

    def test_flag_on_but_not_cloud_managed_stays_local(self) -> None:
        """Tenant gate requires BOTH agent_managed_by=='cloud' AND the flag —
        a local (non-cloud) agent stays LOCAL even with the flag ON."""
        set_current_cycle_agent("agent-a")
        hook = _make_hook(access_scope_repo=None)  # no scope => managed_by=None

        with (
            patch(
                "hermes.runtime.security_hook._tenant_remote_approval_enabled",
                return_value=True,
            ),
            patch(
                "hermes.runtime.security_hook._resolve_native_danger_approval",
                return_value=None,
            ) as mock_native_danger,
        ):
            _run_hook(hook, _MOST_DELICATE_TOOL, {})

        assert mock_native_danger.call_args.kwargs["route"] is ApprovalRoute.LOCAL


# ---------------------------------------------------------------------------
# Fail-soft: a routing bug degrades to LOCAL, never widens/skips the gate
# ---------------------------------------------------------------------------


class TestRoutingFailsSoftToLocal:
    def test_resolve_agent_managed_by_fails_soft_on_raising_repo(self) -> None:
        """Direct unit test of the existing helper in isolation — a raising
        repo must degrade to None (=> never cloud-gated), never raise."""
        from hermes.runtime.security_hook import _resolve_agent_managed_by

        set_current_cycle_agent("agent-a")
        result = _resolve_agent_managed_by(_RaisingAccessScopeRepo(), _TENANT_ID)
        assert result is None

    def test_router_raising_falls_back_to_local_and_gate_still_blocks(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raising approval_router.route() must NEVER turn into an ALLOW —
        the existing native-danger gate keeps blocking exactly as before,
        just with route defaulted to LOCAL."""
        set_current_cycle_agent("agent-a")
        hook = _make_hook(_FakeAccessScopeRepo(scope=_cloud_scope()))

        with (
            caplog.at_level(logging.WARNING, logger="hermes.runtime.security_hook"),
            patch(
                "hermes.capabilities.approval_router.route",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "hermes.runtime.security_hook._resolve_native_danger_approval",
                return_value="still pending owner approval",
            ) as mock_native_danger,
        ):
            result = _run_hook(hook, _MOST_DELICATE_TOOL, {})

        assert mock_native_danger.call_args.kwargs["route"] is ApprovalRoute.LOCAL
        assert result is not None and result.get("action") == "block"
        failure_logs = [
            r for r in caplog.records
            if r.message.startswith(
                "hermes.security_hook.pre.danger_route_consult_failed"
            )
        ]
        assert len(failure_logs) == 1
