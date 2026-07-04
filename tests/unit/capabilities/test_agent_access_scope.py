"""Tests for AgentAccessScope aggregate — Enterprise Fase 2 Phase 1.

Covers: required invariants, factory defaults (enforced=False, cerebro_unrestricted=
True — zero regression), the native-tool allow-set floor, and to_dict (no
credentials).
"""

from __future__ import annotations

import pytest

from hermes.capabilities.domain.agent_access_scope import AgentAccessScope


def _make_scope(**overrides) -> AgentAccessScope:
    defaults = dict(
        tenant_id="tenant-x",
        agent_id="agent-a",
        updated_by=1001,
    )
    defaults.update(overrides)
    return AgentAccessScope.create(**defaults)


# ---------------------------------------------------------------------------
# Creation / invariants
# ---------------------------------------------------------------------------


class TestAgentAccessScopeCreate:
    def test_creates_with_zero_regression_defaults(self):
        scope = _make_scope()
        assert scope.enforced is False
        assert scope.cerebro_unrestricted is True
        assert scope.native_tools == frozenset()
        assert scope.policy_overlay == {}
        assert scope.views == ()
        assert scope.managed_by is None

    def test_scope_id_generated_and_unique(self):
        s1 = _make_scope()
        s2 = _make_scope()
        assert s1.scope_id != s2.scope_id

    def test_updated_by_from_uid(self):
        scope = _make_scope(updated_by=42)
        assert scope.updated_by == 42

    def test_tenant_required(self):
        with pytest.raises(ValueError, match="tenant_id"):
            AgentAccessScope(
                scope_id="sid", tenant_id="", agent_id="a", updated_by=1
            )

    def test_agent_required(self):
        with pytest.raises(ValueError, match="agent_id"):
            AgentAccessScope(
                scope_id="sid", tenant_id="t", agent_id="", updated_by=1
            )

    def test_scope_id_required(self):
        with pytest.raises(ValueError, match="scope_id"):
            AgentAccessScope(
                scope_id="", tenant_id="t", agent_id="a", updated_by=1
            )

    def test_native_tools_must_be_frozenset(self):
        with pytest.raises(TypeError, match="frozenset"):
            AgentAccessScope(
                scope_id="sid",
                tenant_id="t",
                agent_id="a",
                updated_by=1,
                native_tools={"read_file"},  # a plain set, not frozenset
            )

    def test_views_must_be_tuple(self):
        with pytest.raises(TypeError, match="tuple"):
            AgentAccessScope(
                scope_id="sid",
                tenant_id="t",
                agent_id="a",
                updated_by=1,
                views=["dashboard"],
            )


# ---------------------------------------------------------------------------
# allows_native_tool — the enforcement floor
# ---------------------------------------------------------------------------


class TestAllowsNativeTool:
    def test_unenforced_allows_everything(self):
        scope = _make_scope(enforced=False, native_tools=frozenset({"read_file"}))
        assert scope.allows_native_tool("terminal") is True
        assert scope.allows_native_tool("write_file") is True

    def test_enforced_allows_only_listed_tools(self):
        scope = _make_scope(enforced=True, native_tools=frozenset({"read_file"}))
        assert scope.allows_native_tool("read_file") is True
        assert scope.allows_native_tool("terminal") is False
        assert scope.allows_native_tool("write_file") is False

    def test_enforced_empty_allow_set_blocks_all_native_tools(self):
        scope = _make_scope(enforced=True, native_tools=frozenset())
        assert scope.allows_native_tool("read_file") is False


# ---------------------------------------------------------------------------
# to_dict — no PII/credentials, D-Bus transport shape
# ---------------------------------------------------------------------------


class TestToDict:
    def test_to_dict_contains_required_keys(self):
        scope = _make_scope(native_tools=frozenset({"read_file"}), views=("dashboard",))
        d = scope.to_dict()
        assert d["scope_id"] == scope.scope_id
        assert d["agent_id"] == "agent-a"
        assert d["tenant_id"] == "tenant-x"
        assert d["native_tools"] == ["read_file"]
        assert d["views"] == ["dashboard"]
        assert d["enforced"] is False
        assert d["cerebro_unrestricted"] is True
        assert d["updated_by"] == 1001

    def test_to_dict_does_not_contain_credentials(self):
        scope = _make_scope()
        d = scope.to_dict()
        for key in ("password", "token", "secret", "api_key"):
            assert key not in d
