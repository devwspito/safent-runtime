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


def _make_hook(access_scope_repo, tenant_id: str = _TENANT_ID, agent_registry=None):
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
        agent_registry=agent_registry,
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

    def test_non_native_tool_passes_when_scope_is_unenforced(self) -> None:
        set_current_cycle_agent("agent-a")
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id="agent-a", updated_by=1,
            enforced=False, native_tools=frozenset(),
        )
        hook = _make_hook(_FakeAccessScopeRepo(scope=scope))
        assert _run_hook(hook, "gmail_send_email", {}) is None

    def test_non_native_tool_passes_when_no_scope_row(self) -> None:
        set_current_cycle_agent("agent-a")
        hook = _make_hook(_FakeAccessScopeRepo(scope=None))
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
# H-1 (2026-07-07, security review) — dispatch-time backstop for the THIRD
# tool class (os_surface). The PRIMARY fix is presentation-time
# (nous_engine._filter_mcp_skill, see test_nous_engine_agent_access_scope.py
# TestOsSurfaceLockdownH1); this is the belt-and-suspenders floor.
# ---------------------------------------------------------------------------


class TestNonNativeToolBackstopH1:
    def _locked_repo(self, authorized_mcp: frozenset[str] = frozenset()) -> _FakeAccessScopeRepo:
        scope = AgentAccessScope.create(
            tenant_id=_TENANT_ID, agent_id=DEFAULT_AGENT_ID, updated_by=1,
            enforced=True, native_tools=frozenset(), cerebro_unrestricted=False,
            authorized_mcp_servers=authorized_mcp,
        )
        return _FakeAccessScopeRepo(scope=scope)

    def test_os_surface_tool_blocked_under_locked_scope(self) -> None:
        set_current_cycle_agent(DEFAULT_AGENT_ID)
        hook = _make_hook(self._locked_repo())
        result = _run_hook(hook, "delegate_to_colleague", {})
        assert result is not None
        assert result.get("action") == "block"

    def test_lo_write_text_blocked_under_locked_scope(self) -> None:
        set_current_cycle_agent(DEFAULT_AGENT_ID)
        hook = _make_hook(self._locked_repo())
        result = _run_hook(hook, "lo_write_text", {})
        assert result is not None

    def test_authorized_mcp_tool_still_passes_under_locked_scope(self) -> None:
        """The dispatch-time backstop must never collaterally block a
        legitimately bundle-authorized MCP tool — SC-002 requires management
        tools to keep working for the locked Cerebro."""
        set_current_cycle_agent(DEFAULT_AGENT_ID)
        hook = _make_hook(self._locked_repo(authorized_mcp=frozenset({"safent-control"})))
        result = _run_hook(hook, "mcp__safent-control__list_employees", {})
        assert result is None

    def test_unauthorized_mcp_slug_blocked_under_locked_scope(self) -> None:
        set_current_cycle_agent(DEFAULT_AGENT_ID)
        hook = _make_hook(self._locked_repo(authorized_mcp=frozenset({"safent-control"})))
        result = _run_hook(hook, "mcp__evil-server__steal", {})
        assert result is not None
        assert result.get("action") == "block"


# ---------------------------------------------------------------------------
# H-1 residual (2026-07-07, security review, Medium) — a managed/cloud
# DEFAULT_AGENT_ID with a MISSING scope row must be LOCKED, not omnipotent.
# ---------------------------------------------------------------------------


class _CloudManagedAgent:
    managed_by = "cloud"


class _LocalAgent:
    managed_by = None


class _FakeAgentRegistry:
    def __init__(self, agent) -> None:
        self._agent = agent

    def get_agent(self, agent_id: str):
        return self._agent


class _RaisingAgentRegistry:
    def get_agent(self, agent_id: str):
        raise RuntimeError("registry unavailable")


class TestMissingScopeRowFailClosedForCloudManagedCerebro:
    def test_managed_default_agent_missing_scope_is_locked(self) -> None:
        """End-to-end through the real hook (agent_registry threaded through
        register_security_hooks/make_pre_tool_call_hook)."""
        set_current_cycle_agent(DEFAULT_AGENT_ID)
        hook = _make_hook(
            _FakeAccessScopeRepo(scope=None),
            agent_registry=_FakeAgentRegistry(_CloudManagedAgent()),
        )
        result = _run_hook(hook, "delegate_to_colleague", {})
        assert result is not None, (
            "a managed/cloud Cerebro with no resolvable scope row must be "
            "LOCKED — reverting to omnipotence is exactly the H-1 residual"
        )
        assert result.get("action") == "block"

    def test_ordinary_ce_default_agent_missing_scope_stays_open(self) -> None:
        """Zero regression: an unmanaged CE install (no registry signal, or
        managed_by is None) keeps today's fail-open behaviour."""
        from hermes.runtime.security_hook import _check_agent_access_scope

        set_current_cycle_agent(DEFAULT_AGENT_ID)
        result = _check_agent_access_scope(
            "delegate_to_colleague",
            _FakeAccessScopeRepo(scope=None),
            _TENANT_ID,
            _FakeAgentRegistry(_LocalAgent()),
        )
        assert result is None

    def test_no_agent_registry_wired_stays_open(self) -> None:
        """agent_registry is optional (None) — every existing call site that
        doesn't pass it keeps today's fail-open behaviour unchanged."""
        from hermes.runtime.security_hook import _check_agent_access_scope

        set_current_cycle_agent(DEFAULT_AGENT_ID)
        result = _check_agent_access_scope(
            "delegate_to_colleague", _FakeAccessScopeRepo(scope=None), _TENANT_ID, None
        )
        assert result is None

    def test_non_default_agent_missing_scope_stays_open_even_if_cloud_managed(
        self,
    ) -> None:
        """The fail-closed residual is scoped to DEFAULT_AGENT_ID only — a
        cloud-managed CUSTOM agent with no scope row is unaffected (it was
        already fail-closed by _filter_mcp_skill's per-kind gate for
        non-native tools; this residual is specifically the Cerebro-
        omnipotence-bypass gap)."""
        from hermes.runtime.security_hook import _check_agent_access_scope

        set_current_cycle_agent("custom-agent-1")
        result = _check_agent_access_scope(
            "delegate_to_colleague",
            _FakeAccessScopeRepo(scope=None),
            _TENANT_ID,
            _FakeAgentRegistry(_CloudManagedAgent()),
        )
        assert result is None

    def test_registry_lookup_error_stays_open_not_widened_block(self) -> None:
        """_agent_is_cloud_managed fails soft to False on a registry error —
        this only NARROWS the fail-closed branch, never widens a block."""
        from hermes.runtime.security_hook import _check_agent_access_scope

        set_current_cycle_agent(DEFAULT_AGENT_ID)
        result = _check_agent_access_scope(
            "delegate_to_colleague",
            _FakeAccessScopeRepo(scope=None),
            _TENANT_ID,
            _RaisingAgentRegistry(),
        )
        assert result is None


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
