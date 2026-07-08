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


def _os_surface_spec(name: str) -> ToolSpec:
    """Mirrors capability_tool_specs.build_capability_tool_specs' shape
    exactly: entity_type="os_surface", no tags, WRITE risk (no handler
    required — see runtime/capability_tool_specs.py's handler=None for
    WRITE tools, routed via GovernedAIAgent._dispatch_external_write)."""
    return ToolSpec(
        name=name,
        description=f"capability: {name}",
        parameters_schema={"type": "object", "properties": {}},
        risk=ToolRisk.WRITE_EXECUTE,
        entity_type="os_surface",
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


class TestBundleAuthorizedMcpAdmission:
    """2026-07-07 confused-deputy fix: config-sync cannot call
    BindCapabilityToAgent (D-Bus-denied for uid=hermes). The applier instead
    lands the bundle's MCP allow-set on AgentAccessScope.authorized_mcp_servers
    via set_agent_access_scope; _filter_mcp_skill must admit those MCP tools
    even with zero AgentCapabilityBinding rows."""

    def test_cerebro_locked_scope_admits_bundle_authorized_mcp(self) -> None:
        scope = AgentAccessScope.create(
            tenant_id=str(_TENANT), agent_id=DEFAULT_AGENT_ID, updated_by=1,
            enforced=True, cerebro_unrestricted=False,
            authorized_mcp_servers=frozenset({"safent-control"}),
        )
        engine = _engine(_FakeAccessScopeRepo(scope=scope))
        specs = (_mcp_spec(slug="safent-control", tool="approve"),)
        result = engine._apply_agent_filter(specs, DEFAULT_AGENT_ID)
        assert [s.name for s in result] == ["mcp__safent-control__approve"]

    def test_cerebro_locked_scope_denies_unauthorized_mcp_server(self) -> None:
        scope = AgentAccessScope.create(
            tenant_id=str(_TENANT), agent_id=DEFAULT_AGENT_ID, updated_by=1,
            enforced=True, cerebro_unrestricted=False,
            authorized_mcp_servers=frozenset({"safent-control"}),
        )
        engine = _engine(_FakeAccessScopeRepo(scope=scope))
        specs = (_mcp_spec(slug="other-mcp", tool="do_thing"),)
        result = engine._apply_agent_filter(specs, DEFAULT_AGENT_ID)
        assert result == (), "an MCP server absent from the bundle authorization is denied"

    def test_bundle_authorization_unions_with_dbus_binding_not_replaces(self) -> None:
        """A D-Bus-created binding (interactive operator path) still admits its
        own slug even when the bundle authorizes a DIFFERENT server."""

        class _BoundCapability:
            def __init__(self, kind: str, capability_id: str) -> None:
                self.kind = kind
                self.capability_id = capability_id

        class _Binding:
            def __init__(self, kind: str, capability_id: str) -> None:
                self.capability = _BoundCapability(kind, capability_id)

        class _RepoWithOneBinding:
            def list_by_agent(self, agent_id: str, tenant_id: str) -> list:
                return [_Binding("mcp", "operator-bound-mcp")]

        scope = AgentAccessScope.create(
            tenant_id=str(_TENANT), agent_id="custom-agent-1", updated_by=1,
            authorized_mcp_servers=frozenset({"bundle-authorized-mcp"}),
        )
        engine = NousReasoningEngine(
            persona=_persona(),
            tenant_id=_TENANT,
            capability_binding_repo=_RepoWithOneBinding(),
            access_scope_repo=_FakeAccessScopeRepo(scope=scope),
        )
        specs = (
            _mcp_spec(slug="operator-bound-mcp", tool="a"),
            _mcp_spec(slug="bundle-authorized-mcp", tool="b"),
            _mcp_spec(slug="neither", tool="c"),
        )
        result = engine._apply_agent_filter(specs, "custom-agent-1")
        assert {s.name for s in result} == {
            "mcp__operator-bound-mcp__a",
            "mcp__bundle-authorized-mcp__b",
        }

    def test_access_scope_lookup_error_does_not_grant_extra_mcp(self) -> None:
        """Fail-closed: a broken access_scope lookup must never ADD grants —
        it only ever costs the (separate) CEO omnipotence bypass, never widens
        the fail-closed MCP filter for a non-Cerebro agent."""
        engine = NousReasoningEngine(
            persona=_persona(),
            tenant_id=_TENANT,
            capability_binding_repo=_FakeCapabilityBindingRepo(),
            access_scope_repo=_RaisingAccessScopeRepo(),
        )
        result = engine._apply_agent_filter((_mcp_spec(),), "custom-agent-1")
        assert result == ()


class TestOsSurfaceLockdownH1:
    """H-1 (2026-07-07, security review): the THIRD tool class.

    os_surface capability tools (delegate_to_colleague, lo_*, activate_app,
    ...) were governed by NEITHER the native-tool floor (only governs the
    Nous native catalog) NOR _filter_mcp_skill (only looked at mcp/skill) —
    a LOCKED agent (enforced=True, cerebro_unrestricted=False) got the FULL
    os_surface catalog unfiltered. SC-002 requires this be STRUCTURAL (tool
    absent), not broker-refused.
    """

    def test_locked_cerebro_gets_zero_os_surface_tools(self) -> None:
        scope = AgentAccessScope.create(
            tenant_id=str(_TENANT), agent_id=DEFAULT_AGENT_ID, updated_by=1,
            enforced=True, cerebro_unrestricted=False,
        )
        engine = _engine(_FakeAccessScopeRepo(scope=scope))
        specs = (
            _os_surface_spec("delegate_to_colleague"),
            _os_surface_spec("lo_write_text"),
            _os_surface_spec("activate_app"),
        )
        result = engine._apply_agent_filter(specs, DEFAULT_AGENT_ID)
        assert result == (), "a locked agent must get ZERO os_surface tools"

    def test_delegate_to_colleague_never_admitted_for_a_locked_agent(self) -> None:
        """No allow-set exists yet (default EMPTY) — delegate_to_colleague, a
        literal delegation tool, must never be admittable this way."""
        scope = AgentAccessScope.create(
            tenant_id=str(_TENANT), agent_id=DEFAULT_AGENT_ID, updated_by=1,
            enforced=True, cerebro_unrestricted=False,
            authorized_mcp_servers=frozenset({"safent-control"}),
        )
        engine = _engine(_FakeAccessScopeRepo(scope=scope))
        specs = (
            _os_surface_spec("delegate_to_colleague"),
            _mcp_spec(slug="safent-control", tool="list_employees"),
        )
        result = engine._apply_agent_filter(specs, DEFAULT_AGENT_ID)
        names = [s.name for s in result]
        assert "delegate_to_colleague" not in names
        assert "mcp__safent-control__list_employees" in names, (
            "the locked scope's OWN bundle-authorized MCP tool must survive — "
            "the os_surface gate must not collaterally drop legitimate MCP admission"
        )

    def test_unlocked_cerebro_keeps_every_os_surface_tool(self) -> None:
        """Zero regression: an omnipotent (unmanaged / cerebro_unrestricted)
        agent is untouched by H-1."""
        engine = NousReasoningEngine(
            persona=_persona(),
            tenant_id=_TENANT,
            capability_binding_repo=_FakeCapabilityBindingRepo(),
            access_scope_repo=None,
        )
        specs = (
            _os_surface_spec("delegate_to_colleague"),
            _os_surface_spec("lo_write_text"),
            _os_surface_spec("activate_app"),
        )
        result = engine._apply_agent_filter(specs, DEFAULT_AGENT_ID)
        assert {s.name for s in result} == {
            "delegate_to_colleague", "lo_write_text", "activate_app",
        }

    def test_enforced_false_keeps_os_surface_tools(self) -> None:
        """enforced is the master gate — a scope that governs nothing yet
        must not filter os_surface either (mirrors the MCP/skill gate)."""
        scope = AgentAccessScope.create(
            tenant_id=str(_TENANT), agent_id=DEFAULT_AGENT_ID, updated_by=1,
            enforced=False, cerebro_unrestricted=False,
        )
        engine = _engine(_FakeAccessScopeRepo(scope=scope))
        specs = (_os_surface_spec("delegate_to_colleague"),)
        result = engine._apply_agent_filter(specs, DEFAULT_AGENT_ID)
        assert [s.name for s in result] == ["delegate_to_colleague"]

    def test_locked_custom_agent_also_gets_zero_os_surface_tools(self) -> None:
        """The gate generalizes to any agent_id, not just DEFAULT_AGENT_ID —
        AgentAccessScope is agent-agnostic by design."""
        scope = AgentAccessScope.create(
            tenant_id=str(_TENANT), agent_id="custom-agent-1", updated_by=1,
            enforced=True, cerebro_unrestricted=False,
        )
        engine = NousReasoningEngine(
            persona=_persona(),
            tenant_id=_TENANT,
            capability_binding_repo=_FakeCapabilityBindingRepo(),
            access_scope_repo=_FakeAccessScopeRepo(scope=scope),
        )
        result = engine._apply_agent_filter(
            (_os_surface_spec("lo_write_text"),), "custom-agent-1"
        )
        assert result == ()

    def test_repo_error_fails_open_os_surface_gate(self) -> None:
        """A broken repo must never strand EVERY agent without its os_surface
        tools — fails open on the GATE decision itself (distinct from the
        separate, deliberately fail-CLOSED missing-scope-row case at the
        dispatch floor — see security_hook tests)."""
        engine = NousReasoningEngine(
            persona=_persona(),
            tenant_id=_TENANT,
            capability_binding_repo=_FakeCapabilityBindingRepo(),
            access_scope_repo=_RaisingAccessScopeRepo(),
        )
        result = engine._apply_agent_filter(
            (_os_surface_spec("delegate_to_colleague"),), DEFAULT_AGENT_ID
        )
        assert [s.name for s in result] == ["delegate_to_colleague"]
