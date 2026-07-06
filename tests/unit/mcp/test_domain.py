"""Unit tests for mcp/domain — value objects, entities, and tool classifier.

Tests cover:
  (a) ServerSlug invariants.
  (b) Transport.stdio invariants.
  (c) classify_mcp_tool defaults to HIGH risk.
  (d) readOnlyHint=True + read-only name suffix + non-USER_ADDED → LOW + auto_executable.
  (e) destructiveHint=True always → HIGH.
  (f) USER_ADDED trust_level → always HIGH.
  (g) McpTool.qualified_name format.
  (h) McpServer lifecycle transitions.
"""

from __future__ import annotations

import pytest

from hermes.capabilities.domain.ports import RiskLevel
from hermes.mcp.domain.entities import McpServer, McpTool
from hermes.mcp.domain.tool_classifier import classify_mcp_tool
from hermes.mcp.domain.value_objects import (
    McpServerId,
    ServerHealth,
    ServerSlug,
    Transport,
    TrustLevel,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# ServerSlug
# ---------------------------------------------------------------------------


class TestServerSlug:
    def test_valid_slug(self) -> None:
        slug = ServerSlug("playwright-mcp")
        assert str(slug) == "playwright-mcp"

    def test_single_char_slug(self) -> None:
        slug = ServerSlug("a")
        assert str(slug) == "a"

    def test_slug_with_numbers(self) -> None:
        slug = ServerSlug("my-tool-1")
        assert str(slug) == "my-tool-1"

    def test_empty_slug_raises(self) -> None:
        with pytest.raises(ValueError):
            ServerSlug("")

    def test_uppercase_raises(self) -> None:
        with pytest.raises(ValueError):
            ServerSlug("My-Tool")

    def test_leading_dash_raises(self) -> None:
        with pytest.raises(ValueError):
            ServerSlug("-bad")

    def test_trailing_dash_raises(self) -> None:
        with pytest.raises(ValueError):
            ServerSlug("bad-")

    def test_spaces_raise(self) -> None:
        with pytest.raises(ValueError):
            ServerSlug("bad slug")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class TestTransport:
    def test_stdio_factory(self) -> None:
        t = Transport.stdio(["npx", "@playwright/mcp", "--headless"])
        assert t.argv == ("npx", "@playwright/mcp", "--headless")

    def test_empty_argv_raises(self) -> None:
        with pytest.raises(ValueError):
            Transport(argv=())


# ---------------------------------------------------------------------------
# classify_mcp_tool — default HIGH
# ---------------------------------------------------------------------------


class TestClassifyMcpToolDefaults:
    """(c) Default risk is HIGH unless provably read-only."""

    def test_no_hints_returns_high(self) -> None:
        cls = classify_mcp_tool(
            "do_something",
            read_only_hint=None,
            destructive_hint=None,
            trust_level=TrustLevel.BUILTIN,
        )
        assert cls.risk is RiskLevel.HIGH
        assert cls.auto_executable is False

    def test_unknown_name_no_hints_high(self) -> None:
        cls = classify_mcp_tool(
            "create_file",
            trust_level=TrustLevel.USER_TRUSTED,
        )
        assert cls.risk is RiskLevel.HIGH

    def test_read_only_hint_false_stays_high(self) -> None:
        cls = classify_mcp_tool(
            "list_items",
            read_only_hint=False,
            trust_level=TrustLevel.BUILTIN,
        )
        assert cls.risk is RiskLevel.HIGH


class TestClassifyMcpToolReadOnly:
    """(d) readOnlyHint=True + suffix in read-only set → LOW."""

    def test_read_hint_and_read_suffix_builtin(self) -> None:
        cls = classify_mcp_tool(
            "resource_list",
            read_only_hint=True,
            trust_level=TrustLevel.BUILTIN,
        )
        assert cls.risk is RiskLevel.LOW
        assert cls.auto_executable is True

    def test_read_hint_user_trusted_not_auto_exec_if_get(self) -> None:
        cls = classify_mcp_tool(
            "data_get",
            read_only_hint=True,
            trust_level=TrustLevel.USER_TRUSTED,
        )
        assert cls.risk is RiskLevel.LOW
        assert cls.auto_executable is True

    def test_read_hint_but_write_suffix_still_high(self) -> None:
        cls = classify_mcp_tool(
            "email_send",
            read_only_hint=True,
            trust_level=TrustLevel.BUILTIN,
        )
        assert cls.risk is RiskLevel.HIGH

    def test_read_hint_with_no_suffix_match_stays_high(self) -> None:
        cls = classify_mcp_tool(
            "process_data",
            read_only_hint=True,
            trust_level=TrustLevel.BUILTIN,
        )
        assert cls.risk is RiskLevel.HIGH


class TestClassifyMcpToolDestructive:
    """(e) destructiveHint=True → always HIGH regardless of other hints."""

    def test_destructive_overrides_read_hint(self) -> None:
        cls = classify_mcp_tool(
            "resource_list",
            read_only_hint=True,
            destructive_hint=True,
            trust_level=TrustLevel.BUILTIN,
        )
        assert cls.risk is RiskLevel.HIGH
        assert cls.auto_executable is False


class TestClassifyMcpToolUserAdded:
    """(f) USER_ADDED → always HIGH regardless of hints."""

    def test_user_added_always_high(self) -> None:
        cls = classify_mcp_tool(
            "resource_get",
            read_only_hint=True,
            destructive_hint=None,
            trust_level=TrustLevel.USER_ADDED,
        )
        assert cls.risk is RiskLevel.HIGH
        assert cls.auto_executable is False


# ---------------------------------------------------------------------------
# classify_mcp_tool — MANAGED_REMOTE (first-party, egresses to a managed
# control-plane, e.g. safent-control). Reads fluid; writes LOW+not-auto so
# CTRL-5 (requires_forced_hitl) bites the instant the cycle is tainted.
# ---------------------------------------------------------------------------


class TestClassifyMcpToolManagedRemote:
    """MANAGED_REMOTE classification is purely name-driven (no hint dependency)."""

    @pytest.mark.parametrize("name", ["list_agents", "get_usage", "resource_fetch"])
    def test_read_verb_is_low_and_auto(self, name: str) -> None:
        cls = classify_mcp_tool(name, trust_level=TrustLevel.MANAGED_REMOTE)
        assert cls.risk is RiskLevel.LOW
        assert cls.auto_executable is True

    @pytest.mark.parametrize("name", ["create_employee", "delete_agent", "update_billing"])
    def test_write_verb_is_low_but_not_auto(self, name: str) -> None:
        cls = classify_mcp_tool(name, trust_level=TrustLevel.MANAGED_REMOTE)
        assert cls.risk is RiskLevel.LOW
        assert cls.auto_executable is False

    def test_destructive_hint_forces_high_even_for_managed_remote(self) -> None:
        cls = classify_mcp_tool(
            "list_agents",
            destructive_hint=True,
            trust_level=TrustLevel.MANAGED_REMOTE,
        )
        assert cls.risk is RiskLevel.HIGH
        assert cls.auto_executable is False

    def test_read_only_hint_is_irrelevant_for_managed_remote(self) -> None:
        """Unlike BUILTIN/USER_TRUSTED, MANAGED_REMOTE never consults read_only_hint."""
        cls = classify_mcp_tool(
            "delete_agent",
            read_only_hint=True,
            trust_level=TrustLevel.MANAGED_REMOTE,
        )
        assert cls.risk is RiskLevel.LOW
        assert cls.auto_executable is False


class TestClassifyMcpToolManagedRemoteDoesNotAffectOtherTiers:
    """Regression: adding MANAGED_REMOTE must not change BUILTIN/USER_ADDED output."""

    def test_builtin_excel_style_tool_unchanged(self) -> None:
        cls = classify_mcp_tool("workbook_write", trust_level=TrustLevel.BUILTIN)
        assert cls.risk is RiskLevel.LOW
        assert cls.auto_executable is True

    def test_user_added_write_tool_unchanged(self) -> None:
        cls = classify_mcp_tool(
            "create_employee",
            trust_level=TrustLevel.USER_ADDED,
        )
        assert cls.risk is RiskLevel.HIGH
        assert cls.auto_executable is False


# ---------------------------------------------------------------------------
# McpTool entity
# ---------------------------------------------------------------------------


class TestMcpTool:
    """(g) McpTool.qualified_name format."""

    def test_qualified_name_format(self) -> None:
        slug = ServerSlug("playwright-mcp")
        tool = McpTool.build(
            name="browser_navigate",
            description="Navigate to URL",
            slug=slug,
            trust_level=TrustLevel.BUILTIN,
        )
        assert tool.qualified_name == "mcp__playwright-mcp__browser_navigate"

    def test_tool_inherits_server_trust_classification(self) -> None:
        slug = ServerSlug("my-server")
        tool = McpTool.build(
            name="data_list",
            description="List items",
            slug=slug,
            trust_level=TrustLevel.USER_ADDED,
            read_only_hint=True,
        )
        # USER_ADDED → always HIGH regardless of readOnlyHint
        assert tool.risk is RiskLevel.HIGH
        assert tool.auto_executable is False


# ---------------------------------------------------------------------------
# McpServer aggregate
# ---------------------------------------------------------------------------


class TestMcpServer:
    """(h) McpServer lifecycle transitions."""

    def _make_server(self) -> McpServer:
        return McpServer(
            server_id=McpServerId.generate(),
            slug=ServerSlug("test-server"),
            transport=Transport.stdio(["npx", "test-mcp"]),
            trust_level=TrustLevel.BUILTIN,
        )

    def test_initial_health_is_connecting(self) -> None:
        server = self._make_server()
        assert server.health is ServerHealth.CONNECTING

    def test_mark_healthy_sets_tools(self) -> None:
        server = self._make_server()
        slug = ServerSlug("test-server")
        tool = McpTool.build(
            name="my_list", description="", slug=slug, trust_level=TrustLevel.BUILTIN,
            read_only_hint=True,
        )
        server.mark_healthy([tool])
        assert server.health is ServerHealth.HEALTHY
        assert len(server.tools) == 1

    def test_record_restart_below_limit_stays_non_failed(self) -> None:
        server = self._make_server()
        server.record_restart()
        assert server.health is not ServerHealth.FAILED
        assert server.restart_count == 1

    def test_record_restart_beyond_limit_becomes_failed(self) -> None:
        server = self._make_server()
        for _ in range(6):
            server.record_restart()
        assert server.health is ServerHealth.FAILED

    def test_get_tool_returns_none_for_missing(self) -> None:
        server = self._make_server()
        assert server.get_tool("nonexistent") is None

    def test_get_tool_finds_existing(self) -> None:
        server = self._make_server()
        slug = ServerSlug("test-server")
        tool = McpTool.build(
            name="resource_get", description="", slug=slug, trust_level=TrustLevel.BUILTIN,
            read_only_hint=True,
        )
        server.mark_healthy([tool])
        found = server.get_tool("resource_get")
        assert found is not None
        assert found.name == "resource_get"
