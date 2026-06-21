"""Unit tests for McpSurfaceAdapter.

Tests cover:
  (a) replay() succeeds when server is connected and tool exists.
  (b) replay() returns REJECTED_BY_POLICY on surface_kind mismatch.
  (c) replay() returns REJECTED_BY_POLICY on missing server_id.
  (d) replay() returns REJECTED_BY_POLICY on missing tool_name.
  (e) replay() returns REJECTED_BY_POLICY on unknown server.
  (f) replay() result includes is_external_content=True (CTRL-5 taint signal).
  (g) capture() raises NotImplementedError.
  (h) serialize_for_signing() produces stable bytes.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import CapturedAction, ReplayStatus
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.mcp.application.mcp_server_manager import McpServerManager
from hermes.mcp.domain.entities import McpServer, McpTool
from hermes.mcp.domain.value_objects import McpServerId, ServerSlug, Transport, TrustLevel
from hermes.mcp.infrastructure.mcp_surface_adapter import McpSurfaceAdapter

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SucceedingFakeClient:
    async def initialize(self) -> None: ...
    async def list_tools(self) -> list[dict[str, Any]]: return []
    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"result": f"ok_{name}"}
    async def close(self) -> None: ...


def _make_connected_manager(sid: McpServerId, slug_str: str) -> McpServerManager:
    """Inject a pre-connected server directly (no async connect needed)."""
    slug = ServerSlug(slug_str)
    tool = McpTool.build(
        name="resource_list",
        description="",
        slug=slug,
        trust_level=TrustLevel.BUILTIN,
        read_only_hint=True,
    )
    server = McpServer(
        server_id=sid,
        slug=slug,
        transport=Transport.stdio(["npx", "test-mcp"]),
        trust_level=TrustLevel.BUILTIN,
    )
    server.mark_healthy([tool])

    manager = McpServerManager(client_factory=lambda _t: _SucceedingFakeClient())
    manager._servers[str(sid)] = server
    manager._clients[str(sid)] = _SucceedingFakeClient()  # type: ignore[assignment]
    return manager


def _action(
    surface_kind: SurfaceKind = SurfaceKind.MCP_CALL,
    server_id: str | None = None,
    tool_name: str | None = None,
    args: dict | None = None,
) -> CapturedAction:
    return CapturedAction(
        action_id=uuid4(),
        surface_kind=surface_kind,
        intent_desc="test",
        payload={
            "server_id": server_id,
            "tool_name": tool_name,
            "args": args or {},
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMcpSurfaceAdapterReplay:
    @pytest.mark.asyncio
    async def test_replay_succeeds_for_connected_tool(self) -> None:
        sid = McpServerId.generate()
        manager = _make_connected_manager(sid, "my-server")
        adapter = McpSurfaceAdapter(server_manager=manager)

        outcome = await adapter.replay(
            _action(server_id=str(sid.value), tool_name="resource_list")
        )
        assert outcome.status is ReplayStatus.EXECUTED_OK
        assert outcome.result.get("is_external_content") is True

    @pytest.mark.asyncio
    async def test_replay_rejected_on_surface_kind_mismatch(self) -> None:
        manager = McpServerManager(client_factory=lambda _t: _SucceedingFakeClient())
        adapter = McpSurfaceAdapter(server_manager=manager)

        outcome = await adapter.replay(
            _action(surface_kind=SurfaceKind.FILESYSTEM, server_id="x", tool_name="t")
        )
        assert outcome.status is ReplayStatus.REJECTED_BY_POLICY

    @pytest.mark.asyncio
    async def test_replay_rejected_on_missing_server_id(self) -> None:
        manager = McpServerManager(client_factory=lambda _t: _SucceedingFakeClient())
        adapter = McpSurfaceAdapter(server_manager=manager)

        outcome = await adapter.replay(
            _action(server_id=None, tool_name="resource_list")
        )
        assert outcome.status is ReplayStatus.REJECTED_BY_POLICY

    @pytest.mark.asyncio
    async def test_replay_rejected_on_missing_tool_name(self) -> None:
        sid = McpServerId.generate()
        manager = _make_connected_manager(sid, "my-server")
        adapter = McpSurfaceAdapter(server_manager=manager)

        outcome = await adapter.replay(
            _action(server_id=str(sid.value), tool_name=None)
        )
        assert outcome.status is ReplayStatus.REJECTED_BY_POLICY

    @pytest.mark.asyncio
    async def test_replay_rejected_on_unknown_server(self) -> None:
        manager = McpServerManager(client_factory=lambda _t: _SucceedingFakeClient())
        adapter = McpSurfaceAdapter(server_manager=manager)

        outcome = await adapter.replay(
            _action(
                server_id="00000000-0000-0000-0000-000000000099",
                tool_name="resource_list",
            )
        )
        assert outcome.status is ReplayStatus.REJECTED_BY_POLICY

    @pytest.mark.asyncio
    async def test_result_includes_external_content_taint(self) -> None:
        sid = McpServerId.generate()
        manager = _make_connected_manager(sid, "my-server")
        adapter = McpSurfaceAdapter(server_manager=manager)

        outcome = await adapter.replay(
            _action(server_id=str(sid.value), tool_name="resource_list")
        )
        assert outcome.result.get("is_external_content") is True, (
            "CTRL-5: MCP results must be tagged as external content for taint propagation"
        )

    @pytest.mark.asyncio
    async def test_capture_raises_not_implemented(self) -> None:
        manager = McpServerManager(client_factory=lambda _t: _SucceedingFakeClient())
        adapter = McpSurfaceAdapter(server_manager=manager)

        with pytest.raises(NotImplementedError):
            await adapter.capture(
                intent_desc="x",
                params={},
                tenant_id=uuid4(),
                human_operator_id=uuid4(),
            )

    def test_serialize_for_signing_stable(self) -> None:
        manager = McpServerManager(client_factory=lambda _t: _SucceedingFakeClient())
        adapter = McpSurfaceAdapter(server_manager=manager)
        action = _action(server_id="some-id", tool_name="resource_list")

        b1 = adapter.serialize_for_signing(action)
        b2 = adapter.serialize_for_signing(action)
        assert b1 == b2
        assert len(b1) > 0

    def test_surface_kind_property(self) -> None:
        manager = McpServerManager(client_factory=lambda _t: _SucceedingFakeClient())
        adapter = McpSurfaceAdapter(server_manager=manager)
        assert adapter.surface_kind is SurfaceKind.MCP_CALL
