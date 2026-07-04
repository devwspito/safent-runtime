"""Native per-agent access-scope floor — Enterprise Fase 2 Phase 1.

Covers the NEW hook gate step (security_hook._check_agent_access_scope /
Step 1.1, wired between the kill-switch and the owner-disabled policy step):

  - An agent with an enforced scope + a narrow native_tools allow-set is
    BLOCKED calling a native tool outside that allow-set, and PASSES calling
    one inside it.
  - An agent with NO ambient stamp, or no repo wired, or no scope row, or an
    unenforced scope → fail-OPEN (today's behaviour, zero regression).
  - A repo error inside the gate → fail-CLOSED (BLOCK), never raises (a raise
    would be swallowed by invoke_hook into ALLOW).

Mirrors the TestHookIntegration pattern in test_terminal_self_jailbreak.py:
builds the real hook via make_pre_tool_call_hook and patches only the
kill-switch bridge (no running event loop in this synchronous test).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes.agents.domain.agent import DEFAULT_AGENT_ID
from hermes.capabilities.domain.agent_access_scope import AgentAccessScope
from hermes.runtime.conversation_task_registry import (
    clear_current_cycle_agent,
    set_current_cycle_agent,
)
from hermes.runtime.security_hook import make_pre_tool_call_hook

pytestmark = pytest.mark.unit

_TENANT_ID = "tenant-x"


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

    hook = make_pre_tool_call_hook(
        agent_state=agent_state,
        engine_loop=loop,
        broker=broker,
        access_scope_repo=access_scope_repo,
        tenant_id=tenant_id,
    )
    return hook


def _run_hook(hook, tool_name: str, args: dict | None = None):
    with patch("hermes.runtime.security_hook._check_kill_switch", return_value=False):
        return hook(tool_name=tool_name, args=args or {})


@pytest.fixture(autouse=True)
def _clean_ambient_agent():
    clear_current_cycle_agent()
    yield
    clear_current_cycle_agent()


# ---------------------------------------------------------------------------
# Enforced scope with a narrow allow-set
# ---------------------------------------------------------------------------


class TestEnforcedScopeNativeFloor:
    def _scoped_repo(self) -> _FakeAccessScopeRepo:
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID,
            agent_id="agent-a",
            updated_by=1,
            enforced=True,
            native_tools=frozenset({"read_file"}),
        )
        return _FakeAccessScopeRepo(scope=scope)

    def test_tool_outside_allow_set_is_blocked_terminal(self) -> None:
        set_current_cycle_agent("agent-a")
        hook = _make_hook(self._scoped_repo())
        result = _run_hook(hook, "terminal", {"command": "ls"})
        assert result is not None
        assert result.get("action") == "block"
        assert "ámbito de acceso" in result.get("message", "")

    def test_tool_outside_allow_set_is_blocked_write_file(self) -> None:
        set_current_cycle_agent("agent-a")
        hook = _make_hook(self._scoped_repo())
        result = _run_hook(hook, "write_file", {"path": "/tmp/x", "content": "y"})
        assert result is not None
        assert result.get("action") == "block"

    def test_tool_inside_allow_set_passes(self) -> None:
        set_current_cycle_agent("agent-a")
        hook = _make_hook(self._scoped_repo())
        result = _run_hook(hook, "read_file", {"path": "/tmp/x"})
        assert result is None


# ---------------------------------------------------------------------------
# Fail-OPEN paths — zero regression for every existing/local install
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_no_ambient_agent_everything_passes(self) -> None:
        # No set_current_cycle_agent() call — a non-cycle context.
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id="agent-a", updated_by=1,
            enforced=True, native_tools=frozenset(),
        )
        hook = _make_hook(_FakeAccessScopeRepo(scope=scope))
        assert _run_hook(hook, "terminal", {"command": "ls"}) is None

    def test_no_repo_wired_everything_passes(self) -> None:
        set_current_cycle_agent("agent-a")
        hook = _make_hook(access_scope_repo=None)
        assert _run_hook(hook, "terminal", {"command": "ls"}) is None

    def test_no_scope_row_everything_passes(self) -> None:
        set_current_cycle_agent("agent-a")
        hook = _make_hook(_FakeAccessScopeRepo(scope=None))
        assert _run_hook(hook, "terminal", {"command": "ls"}) is None

    def test_unenforced_scope_everything_passes(self) -> None:
        set_current_cycle_agent("agent-a")
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id="agent-a", updated_by=1,
            enforced=False, native_tools=frozenset(),
        )
        hook = _make_hook(_FakeAccessScopeRepo(scope=scope))
        assert _run_hook(hook, "terminal", {"command": "ls"}) is None

    def test_non_native_tool_is_never_governed_by_this_floor(self) -> None:
        set_current_cycle_agent("agent-a")
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id="agent-a", updated_by=1,
            enforced=True, native_tools=frozenset(),
        )
        hook = _make_hook(_FakeAccessScopeRepo(scope=scope))
        # An external/capability tool name unknown to classify_nous_tool.
        assert _run_hook(hook, "gmail_send_email", {}) is None


# ---------------------------------------------------------------------------
# CEO/Cerebro omnipotence bypass mirrors nous_engine's
# ---------------------------------------------------------------------------


class TestCerebroBypassInHook:
    def test_cerebro_bypasses_enforced_scope_by_default(self) -> None:
        set_current_cycle_agent(DEFAULT_AGENT_ID)
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id=DEFAULT_AGENT_ID, updated_by=1,
            enforced=True, native_tools=frozenset(), cerebro_unrestricted=True,
        )
        hook = _make_hook(_FakeAccessScopeRepo(scope=scope))
        assert _run_hook(hook, "terminal", {"command": "ls"}) is None

    def test_cerebro_unrestricted_false_is_gated_like_a_custom_agent(self) -> None:
        set_current_cycle_agent(DEFAULT_AGENT_ID)
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id=DEFAULT_AGENT_ID, updated_by=1,
            enforced=True, native_tools=frozenset(), cerebro_unrestricted=False,
        )
        hook = _make_hook(_FakeAccessScopeRepo(scope=scope))
        result = _run_hook(hook, "terminal", {"command": "ls"})
        assert result is not None
        assert result.get("action") == "block"


# ---------------------------------------------------------------------------
# Fail-CLOSED — a repo error must BLOCK, never raise (would ALLOW via
# invoke_hook's swallow-into-ALLOW behaviour).
# ---------------------------------------------------------------------------


class TestFailClosedOnRepoError:
    def test_repo_error_blocks_never_raises(self) -> None:
        set_current_cycle_agent("agent-a")
        hook = _make_hook(_RaisingAccessScopeRepo())
        result = _run_hook(hook, "terminal", {"command": "ls"})
        assert result is not None
        assert result.get("action") == "block"
        assert "fail-closed" in result.get("message", "")


# ---------------------------------------------------------------------------
# Caged-exec alias grants (review LOW): the terminal aliases
# (run_command/run_terminal/terminal/process) and code aliases
# (execute_code/run_code) are each the SAME confined surface — granting ANY name
# in a group grants the whole group, so an owner allow-listing 'terminal' is not
# false-blocked when the model calls 'run_command'. Isolated at the Step-1.1
# helper so later hook steps (hardline/command guards) don't confound the check.
# ---------------------------------------------------------------------------


class TestCagedExecAliasGrants:
    def _repo(self, granted: set[str]) -> _FakeAccessScopeRepo:
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id="agent-a", updated_by=1,
            enforced=True, native_tools=frozenset(granted),
        )
        return _FakeAccessScopeRepo(scope=scope)

    @pytest.mark.parametrize("granted,called", [
        ("terminal", "run_command"),
        ("run_command", "terminal"),
        ("terminal", "run_terminal"),
        ("terminal", "process"),
        ("execute_code", "run_code"),
        ("run_code", "execute_code"),
    ])
    def test_granting_one_exec_alias_grants_the_group(self, granted: str, called: str) -> None:
        from hermes.runtime.security_hook import _check_agent_access_scope
        set_current_cycle_agent("agent-a")
        assert _check_agent_access_scope(called, self._repo({granted}), _TENANT_ID) is None, (
            f"granting '{granted}' must allow the alias '{called}' (same confined surface)"
        )

    def test_terminal_grant_does_not_leak_to_code_group(self) -> None:
        from hermes.runtime.security_hook import _check_agent_access_scope
        set_current_cycle_agent("agent-a")
        assert _check_agent_access_scope("execute_code", self._repo({"terminal"}), _TENANT_ID) is not None, (
            "a terminal grant must NOT grant the execute-code group"
        )


# ---------------------------------------------------------------------------
# Native-tool recognition coverage (review LOW): every caged-exec alias must be
# RECOGNIZED as native so an enforced scope actually governs it — a name that
# slipped past _is_native_nous_tool would silently fail-open past the floor.
# ---------------------------------------------------------------------------


class TestNativeToolRecognitionCoverage:
    def test_all_caged_exec_aliases_are_recognized_native(self) -> None:
        from hermes.runtime.security_hook import (
            _CAGED_NATIVE_ALIASES,
            _is_native_nous_tool,
        )
        for name in _CAGED_NATIVE_ALIASES:
            assert _is_native_nous_tool(name), (
                f"caged-exec alias '{name}' must be recognized as native so the "
                "access-scope floor governs it (else it silently fails-open)"
            )
