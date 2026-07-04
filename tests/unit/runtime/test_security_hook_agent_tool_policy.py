"""security_hook per-agent tool-policy overlay — Enterprise Fase 2 Phase 2.

Covers the NEW Step 1.4 (_resolve_tool_policy_for_cycle) wired into Steps
1.5 (is_owner_disabled) and 1.6 (mfa_on_dangers / is_enabled):

  - An agent whose AgentAccessScope carries a policy_overlay disabling a tool
    the GLOBAL store enables -> BLOCKED for that agent only (Step 1.5).
  - No scope row / no ambient agent / no repo / an empty overlay -> global
    behaviour, unaffected (zero regression).
  - A scope-repo error while resolving the per-agent policy -> BLOCKS, never
    raises (fail-closed, mirrors Step 1.1's own posture in
    tests/security/test_agent_access_scope_floor.py).
  - RESTRICT-ONLY sovereignty fix: an overlay {"enabled": True} can NEVER
    un-block a tool the OWNER consciously disabled at the global store —
    Step 1.5 still BLOCKS (F1 regression).

Uses a synthetic, non-native tool name ("custom_test_tool") so Step 1.1
(native access-scope floor) and Step 1.6's native MFA gate never interfere:
classify_nous_tool returns None for it, which is the documented no-op path
for both (hook_mfa_block short-circuits to False; the access-scope floor only
governs native Nous tools).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hermes.capabilities.tool_policy as tool_policy_mod
from hermes.capabilities.domain.agent_access_scope import AgentAccessScope
from hermes.capabilities.tool_policy import ToolPolicyStore
from hermes.runtime.conversation_task_registry import (
    clear_current_cycle_agent,
    set_current_cycle_agent,
)
from hermes.runtime.security_hook import make_pre_tool_call_hook

pytestmark = pytest.mark.unit

_TENANT_ID = "tenant-x"
_TOOL = "custom_test_tool"


class _FakeAccessScopeRepo:
    def __init__(self, scope: AgentAccessScope | None) -> None:
        self._scope = scope
        self.calls: list[tuple[str, str]] = []

    def get_scope(self, agent_id: str, tenant_id: str) -> AgentAccessScope | None:
        self.calls.append((agent_id, tenant_id))
        return self._scope


class _RaisingAccessScopeRepo:
    def get_scope(self, agent_id: str, tenant_id: str) -> AgentAccessScope | None:
        raise RuntimeError("db unavailable")


def _make_hook(access_scope_repo, tenant_id: str = _TENANT_ID):
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


def _make_hook_with_base_store(
    access_scope_repo, base_store: ToolPolicyStore, tenant_id: str = _TENANT_ID
):
    """Build the hook with a CONTROLLED global ToolPolicyStore.

    make_pre_tool_call_hook() does `ToolPolicyStore()` (default owner-only
    path) internally — patch the class it re-imports at call time so the
    test can pin the owner's global decision (e.g. a conscious disable)
    without touching /var/lib/hermes.
    """
    agent_state = MagicMock()
    agent_state.is_paused = AsyncMock(return_value=False)
    loop = asyncio.new_event_loop()
    broker = MagicMock()
    broker._os_native_dispatcher = None  # skip denylist check

    with patch.object(tool_policy_mod, "ToolPolicyStore", return_value=base_store):
        return make_pre_tool_call_hook(
            agent_state=agent_state,
            engine_loop=loop,
            broker=broker,
            access_scope_repo=access_scope_repo,
            tenant_id=tenant_id,
        )


def _run_hook(hook, tool_name: str = _TOOL, args: dict | None = None):
    with patch("hermes.runtime.security_hook._check_kill_switch", return_value=False):
        return hook(tool_name=tool_name, args=args or {})


@pytest.fixture(autouse=True)
def _clean_ambient_agent():
    clear_current_cycle_agent()
    yield
    clear_current_cycle_agent()


# ---------------------------------------------------------------------------
# Overlay disables a tool the global store enables -> blocked for THAT agent
# ---------------------------------------------------------------------------


class TestOverlayBlocksForThisAgentOnly:
    def test_overlay_disables_tool_the_global_enables(self) -> None:
        set_current_cycle_agent("agent-a")
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id="agent-a", updated_by=1,
            policy_overlay={_TOOL: {"enabled": False}},
        )
        hook = _make_hook(_FakeAccessScopeRepo(scope=scope))
        result = _run_hook(hook)
        assert result is not None
        assert result.get("action") == "block"
        assert "Seguridad/Políticas" in result.get("message", "")

    def test_other_agent_without_a_scope_row_is_unaffected(self) -> None:
        """Same tool, a DIFFERENT agent with no scope row -> allowed."""
        set_current_cycle_agent("agent-b")
        hook = _make_hook(_FakeAccessScopeRepo(scope=None))
        assert _run_hook(hook) is None


# ---------------------------------------------------------------------------
# No overlay -> global behaviour, unaffected (zero regression)
# ---------------------------------------------------------------------------


class TestNoOverlayFallsBackToGlobalBehaviour:
    def test_no_ambient_agent_uses_global_store(self) -> None:
        hook = _make_hook(_FakeAccessScopeRepo(scope=None))
        assert _run_hook(hook) is None

    def test_scope_with_empty_overlay_uses_global_store(self) -> None:
        set_current_cycle_agent("agent-a")
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id="agent-a", updated_by=1,
            policy_overlay={},
        )
        hook = _make_hook(_FakeAccessScopeRepo(scope=scope))
        assert _run_hook(hook) is None

    def test_no_scope_row_uses_global_store(self) -> None:
        set_current_cycle_agent("agent-a")
        hook = _make_hook(_FakeAccessScopeRepo(scope=None))
        assert _run_hook(hook) is None

    def test_no_repo_wired_uses_global_store(self) -> None:
        set_current_cycle_agent("agent-a")
        hook = _make_hook(access_scope_repo=None)
        assert _run_hook(hook) is None


# ---------------------------------------------------------------------------
# Fail-CLOSED — a repo error resolving the per-agent policy must BLOCK, never
# raise (would be swallowed by invoke_hook into ALLOW).
# ---------------------------------------------------------------------------


class TestFailClosedOnResolutionError:
    def test_repo_error_blocks_never_raises(self) -> None:
        set_current_cycle_agent("agent-a")
        hook = _make_hook(_RaisingAccessScopeRepo())
        result = _run_hook(hook)
        assert result is not None
        assert result.get("action") == "block"
        assert "fail-closed" in result.get("message", "")


# ---------------------------------------------------------------------------
# F1 sovereignty fix — RESTRICT-ONLY: the cloud overlay can NEVER re-enable a
# tool the OWNER consciously disabled at the global store. Step 1.5 must keep
# blocking even when the overlay says {"enabled": True}.
# ---------------------------------------------------------------------------


class TestOverlayCannotOverrideOwnerDisableAtHookLevel:
    def test_step_1_5_still_blocks_owner_disabled_tool_despite_overlay_enable(
        self, tmp_path: Path
    ) -> None:
        base_store = ToolPolicyStore(path=tmp_path / "tool_policy.json")
        base_store.set_tool(_TOOL, False)  # owner CONSCIOUSLY disabled it

        set_current_cycle_agent("agent-a")
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id="agent-a", updated_by=1,
            policy_overlay={_TOOL: {"enabled": True}},  # cloud tries to re-enable
        )
        hook = _make_hook_with_base_store(
            _FakeAccessScopeRepo(scope=scope), base_store
        )

        result = _run_hook(hook)

        assert result is not None
        assert result.get("action") == "block"
        assert "Seguridad/Políticas" in result.get("message", "")

    def test_overlay_false_still_narrows_an_owner_enabled_tool(
        self, tmp_path: Path
    ) -> None:
        """Legit narrowing case: overlay {"enabled": False} on a globally
        ENABLED tool still blocks — confirms the fix didn't break the
        legitimate restrict direction."""
        base_store = ToolPolicyStore(path=tmp_path / "tool_policy.json")
        base_store.set_tool(_TOOL, True)  # owner leaves it enabled globally

        set_current_cycle_agent("agent-a")
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id="agent-a", updated_by=1,
            policy_overlay={_TOOL: {"enabled": False}},
        )
        hook = _make_hook_with_base_store(
            _FakeAccessScopeRepo(scope=scope), base_store
        )

        result = _run_hook(hook)

        assert result is not None
        assert result.get("action") == "block"
