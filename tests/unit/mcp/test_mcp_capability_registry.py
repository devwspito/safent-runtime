"""Unit tests for McpCapabilityRegistry.

Tests cover:
  (a) Static registry takes priority over MCP resolution.
  (b) Known mcp__<slug>__<tool> resolves to MCP_CALL binding with executor='mcp'.
  (c) Unknown server (slug not in manager) → None (broker fail-closes).
  (d) Non-MCP tool name → None (delegated, not resolved).
  (e) Malformed qualified name → None.
  (f) Resolved binding has correct risk/auto_executable from tool classification.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.domain.ports import CapabilityBinding, RiskLevel
from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
from hermes.mcp.application.mcp_capability_registry import McpCapabilityRegistry
from hermes.mcp.application.mcp_server_manager import McpServerManager
from hermes.mcp.domain.entities import McpTool
from hermes.mcp.domain.value_objects import McpServerId, ServerHealth, ServerSlug, Transport, TrustLevel

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeMcpClient:
    async def initialize(self) -> None: ...
    async def list_tools(self) -> list[dict[str, Any]]: return []
    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]: return {}
    async def close(self) -> None: ...


def _make_manager_with_server(
    slug_str: str,
    tools: list[McpTool],
    trust_level: TrustLevel = TrustLevel.BUILTIN,
) -> McpServerManager:
    """Directly inject a connected server into the manager (bypasses async connect)."""
    from hermes.mcp.domain.entities import McpServer

    manager = McpServerManager(client_factory=lambda _t: _FakeMcpClient())
    sid = McpServerId.generate()
    slug = ServerSlug(slug_str)
    server = McpServer(
        server_id=sid,
        slug=slug,
        transport=Transport.stdio(["npx", "test-mcp"]),
        trust_level=trust_level,
    )
    server.mark_healthy(tools)
    manager._servers[str(sid)] = server
    manager._clients[str(sid)] = _FakeMcpClient()  # type: ignore[assignment]
    return manager


def _low_tool(slug_str: str, name: str) -> McpTool:
    return McpTool.build(
        name=name,
        description="",
        slug=ServerSlug(slug_str),
        trust_level=TrustLevel.BUILTIN,
        read_only_hint=True,
    )


def _high_tool(slug_str: str, name: str) -> McpTool:
    return McpTool.build(
        name=name,
        description="",
        slug=ServerSlug(slug_str),
        trust_level=TrustLevel.BUILTIN,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMcpCapabilityRegistryResolution:
    def test_static_takes_priority(self) -> None:
        static = FakeCapabilityRegistry()
        static.register_low("mcp__some-server__resource_list")
        manager = McpServerManager(client_factory=lambda _t: _FakeMcpClient())
        registry = McpCapabilityRegistry(static_registry=static, server_manager=manager)

        binding = registry.resolve("mcp__some-server__resource_list")
        assert binding is not None
        assert binding.executor != "mcp", "Static registry wins over MCP dynamic resolution"

    def test_known_mcp_tool_resolves_mcp_call(self) -> None:
        manager = _make_manager_with_server(
            "playwright-mcp",
            [_low_tool("playwright-mcp", "resource_list")],
        )
        registry = McpCapabilityRegistry(
            static_registry=FakeCapabilityRegistry(),
            server_manager=manager,
        )

        binding = registry.resolve("mcp__playwright-mcp__resource_list")
        assert binding is not None
        assert binding.surface_kind == SurfaceKind.MCP_CALL
        assert binding.executor == "mcp"
        assert binding.tool_name == "mcp__playwright-mcp__resource_list"

    def test_low_risk_tool_resolves_correctly(self) -> None:
        manager = _make_manager_with_server(
            "my-server",
            [_low_tool("my-server", "data_list")],
        )
        registry = McpCapabilityRegistry(
            static_registry=FakeCapabilityRegistry(),
            server_manager=manager,
        )

        binding = registry.resolve("mcp__my-server__data_list")
        assert binding is not None
        assert binding.risk is RiskLevel.LOW
        assert binding.auto_executable is True

    def test_high_risk_tool_resolves_correctly(self) -> None:
        manager = _make_manager_with_server(
            "my-server",
            [_high_tool("my-server", "execute_command")],
        )
        registry = McpCapabilityRegistry(
            static_registry=FakeCapabilityRegistry(),
            server_manager=manager,
        )

        binding = registry.resolve("mcp__my-server__execute_command")
        assert binding is not None
        assert binding.risk is RiskLevel.HIGH
        assert binding.auto_executable is False

    def test_unknown_server_returns_none(self) -> None:
        manager = McpServerManager(client_factory=lambda _t: _FakeMcpClient())
        registry = McpCapabilityRegistry(
            static_registry=FakeCapabilityRegistry(),
            server_manager=manager,
        )

        result = registry.resolve("mcp__no-such-server__some_tool")
        assert result is None, "Unknown server → None (broker fail-closes)"

    def test_non_mcp_name_returns_none(self) -> None:
        manager = McpServerManager(client_factory=lambda _t: _FakeMcpClient())
        registry = McpCapabilityRegistry(
            static_registry=FakeCapabilityRegistry(),
            server_manager=manager,
        )

        result = registry.resolve("gmail_get_email")
        assert result is None

    def test_malformed_qualified_name_returns_none(self) -> None:
        manager = McpServerManager(client_factory=lambda _t: _FakeMcpClient())
        registry = McpCapabilityRegistry(
            static_registry=FakeCapabilityRegistry(),
            server_manager=manager,
        )

        # Only 2 parts instead of 3
        result = registry.resolve("mcp__only-two")
        assert result is None

    def test_unknown_tool_on_known_server_returns_none(self) -> None:
        manager = _make_manager_with_server(
            "known-server",
            [_low_tool("known-server", "data_list")],
        )
        registry = McpCapabilityRegistry(
            static_registry=FakeCapabilityRegistry(),
            server_manager=manager,
        )

        result = registry.resolve("mcp__known-server__nonexistent_tool")
        assert result is None
