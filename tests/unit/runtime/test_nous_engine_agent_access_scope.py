"""CEO/Cerebro scopable via AgentAccessScope — Enterprise Fase 2 Phase 1.

Before this feature, NousReasoningEngine._apply_agent_filter UNCONDITIONALLY
exempted DEFAULT_AGENT_ID (the CEO/Cerebro) from the fail-closed MCP/skill
filter ("el cerebro lo ve y puede todo"). This is now a POLICY-CONDITIONAL
bypass: the CEO stays omnipotent unless an AgentAccessScope row exists for it
with cerebro_unrestricted=False — a local/unmanaged instance (no repo, no
scope row) is UNCHANGED (zero regression). Custom agents are always filtered,
exactly as before.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from hermes.agents.domain.agent import DEFAULT_AGENT_ID
from hermes.capabilities.domain.agent_access_scope import AgentAccessScope
from hermes.domain.tool_spec import ToolRisk, ToolSpec
from hermes.runtime.nous_engine import NousReasoningEngine

pytestmark = pytest.mark.unit

_TENANT = UUID("20000000-0000-0000-0000-000000000001")


def _persona():
    from hermes.prompts.persona import PersonaSpec

    return PersonaSpec(
        name="H",
        role="test-role",
        language="es",
        register="formal",
        primary_mission="testing",
    )


def _mcp_spec(slug: str = "filesystem", tool: str = "list_files") -> ToolSpec:
    qualified = f"mcp__{slug}__{tool}"

    async def _handler(params: dict) -> dict:
        return {}

    return ToolSpec(
        name=qualified,
        description=f"MCP: {qualified}",
        parameters_schema={"type": "object", "properties": {}},
        risk=ToolRisk.READ_ONLY,
        entity_type="mcp",
        handler=_handler,
        tags=("mcp",),
    )


class _FakeCapabilityBindingRepo:
    """No bindings for ANY agent — fail-closed drops every MCP/skill spec."""

    def list_by_agent(self, agent_id: str, tenant_id: str) -> list:
        return []


class _FakeAccessScopeRepo:
    def __init__(self, scope: AgentAccessScope | None) -> None:
        self._scope = scope

    def get_scope(self, agent_id: str, tenant_id: str) -> AgentAccessScope | None:
        return self._scope


class _RaisingAccessScopeRepo:
    def get_scope(self, agent_id: str, tenant_id: str) -> AgentAccessScope | None:
        raise RuntimeError("boom")


def _engine(access_scope_repo: Any) -> NousReasoningEngine:
    return NousReasoningEngine(
        persona=_persona(),
        tenant_id=_TENANT,
        capability_binding_repo=_FakeCapabilityBindingRepo(),
        access_scope_repo=access_scope_repo,
    )


class TestCeroBypassNoScope:
    def test_no_repo_wired_cerebro_stays_omnipotent(self) -> None:
        engine = NousReasoningEngine(
            persona=_persona(),
            tenant_id=_TENANT,
            capability_binding_repo=_FakeCapabilityBindingRepo(),
            access_scope_repo=None,
        )
        result = engine._apply_agent_filter((_mcp_spec(),), DEFAULT_AGENT_ID)
        assert [s.name for s in result] == ["mcp__filesystem__list_files"], (
            "no access_scope_repo wired (local/unmanaged instance) — the CEO "
            "must stay omnipotent, exactly as before this feature"
        )

    def test_no_scope_row_cerebro_stays_omnipotent(self) -> None:
        engine = _engine(_FakeAccessScopeRepo(scope=None))
        result = engine._apply_agent_filter((_mcp_spec(),), DEFAULT_AGENT_ID)
        assert [s.name for s in result] == ["mcp__filesystem__list_files"]


class TestCeroBypassExplicitScope:
    def test_cerebro_unrestricted_true_still_bypasses(self) -> None:
        scope = AgentAccessScope.create(
            tenant_id=str(_TENANT), agent_id=DEFAULT_AGENT_ID, updated_by=1,
            cerebro_unrestricted=True,
        )
        engine = _engine(_FakeAccessScopeRepo(scope=scope))
        result = engine._apply_agent_filter((_mcp_spec(),), DEFAULT_AGENT_ID)
        assert [s.name for s in result] == ["mcp__filesystem__list_files"]

    def test_cerebro_unrestricted_false_is_filtered_like_a_custom_agent(self) -> None:
        scope = AgentAccessScope.create(
            tenant_id=str(_TENANT), agent_id=DEFAULT_AGENT_ID, updated_by=1,
            enforced=True, cerebro_unrestricted=False,
        )
        engine = _engine(_FakeAccessScopeRepo(scope=scope))
        result = engine._apply_agent_filter((_mcp_spec(),), DEFAULT_AGENT_ID)
        assert result == (), (
            "enforced=True + cerebro_unrestricted=False must flow the CEO through "
            "the SAME fail-closed MCP/skill filter as a custom agent"
        )

    def test_cerebro_unrestricted_false_but_unenforced_stays_omnipotent(self) -> None:
        # Review MEDIUM regression: enforced is the master gate. A scope with
        # enforced=False governs NOTHING — the CEO must stay omnipotent even if
        # cerebro_unrestricted=False, so the engine and the native-tool floor agree
        # (no "false lockdown" where external tools are filtered but native RCE isn't).
        scope = AgentAccessScope.create(
            tenant_id=str(_TENANT), agent_id=DEFAULT_AGENT_ID, updated_by=1,
            enforced=False, cerebro_unrestricted=False,
        )
        engine = _engine(_FakeAccessScopeRepo(scope=scope))
        result = engine._apply_agent_filter((_mcp_spec(),), DEFAULT_AGENT_ID)
        assert [s.name for s in result] == ["mcp__filesystem__list_files"], (
            "enforced=False must NOT filter the CEO regardless of cerebro_unrestricted"
        )


class TestCeroBypassFailSafe:
    def test_repo_error_fails_safe_to_omnipotent(self) -> None:
        """A broken repo must NEVER strand the CEO without its own tools."""
        engine = _engine(_RaisingAccessScopeRepo())
        result = engine._apply_agent_filter((_mcp_spec(),), DEFAULT_AGENT_ID)
        assert [s.name for s in result] == ["mcp__filesystem__list_files"]


class TestCustomAgentUnaffected:
    def test_custom_agent_always_filtered_regardless_of_scope(self) -> None:
        scope = AgentAccessScope.create(
            tenant_id=str(_TENANT), agent_id=DEFAULT_AGENT_ID, updated_by=1,
            cerebro_unrestricted=True,
        )
        engine = _engine(_FakeAccessScopeRepo(scope=scope))
        result = engine._apply_agent_filter((_mcp_spec(),), "custom-agent-1")
        assert result == (), "a custom agent has no binding — fail-closed drops the MCP spec"
