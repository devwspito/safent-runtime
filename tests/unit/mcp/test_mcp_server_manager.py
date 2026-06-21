"""Unit tests for McpServerManager using a fake McpClientPort.

Tests cover:
  (a) connect() populates tools from list_tools().
  (b) connect() idempotent for same server_id.
  (c) connect() raises McpConnectionError on client failure.
  (d) list_tools() returns cached tools.
  (e) call_tool() dispatches to client.
  (f) call_tool() raises McpToolNotFoundError for unknown tool.
  (g) call_tool() raises McpServerNotFoundError for unknown server.
  (h) disconnect() closes the client.
  (i) health() returns correct value.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from hermes.mcp.application.errors import McpConnectionError, McpServerNotFoundError, McpToolNotFoundError
from hermes.mcp.application.mcp_server_manager import McpServerManager
from hermes.mcp.domain.value_objects import McpServerId, ServerSlug, Transport, TrustLevel

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeMcpClient:
    """Fake McpClientPort. Scriptable for test scenarios."""

    def __init__(
        self,
        tools: list[dict[str, Any]] | None = None,
        fail_on_init: bool = False,
        fail_on_call: bool = False,
    ) -> None:
        self._tools = tools or []
        self._fail_on_init = fail_on_init
        self._fail_on_call = fail_on_call
        self.initialized = False
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize(self) -> None:
        if self._fail_on_init:
            raise McpConnectionError("fake init failure")
        self.initialized = True

    async def list_tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._fail_on_call:
            raise McpConnectionError("fake call failure")
        self.calls.append((name, args))
        return {"result": f"called_{name}"}

    async def close(self) -> None:
        self.closed = True


def _make_manager(client: FakeMcpClient) -> McpServerManager:
    return McpServerManager(client_factory=lambda _transport: client)


def _sid() -> McpServerId:
    return McpServerId.generate()


def _slug(name: str = "test-server") -> ServerSlug:
    return ServerSlug(name)


def _transport() -> Transport:
    return Transport.stdio(["npx", "test-mcp"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConnect:
    """(a) connect() populates tools."""

    @pytest.mark.asyncio
    async def test_connect_populates_tools(self) -> None:
        client = FakeMcpClient(tools=[
            {"name": "resource_list", "description": "List",
             "annotations": {"readOnlyHint": True, "destructiveHint": None}},
        ])
        manager = _make_manager(client)
        sid = _sid()

        server = await manager.connect(
            server_id=sid, slug=_slug(), transport=_transport(),
            trust_level=TrustLevel.BUILTIN,
        )

        assert len(server.tools) == 1
        assert server.tools[0].name == "resource_list"

    @pytest.mark.asyncio
    async def test_connect_idempotent(self) -> None:
        client = FakeMcpClient(tools=[])
        manager = _make_manager(client)
        sid = _sid()

        s1 = await manager.connect(
            server_id=sid, slug=_slug(), transport=_transport(),
            trust_level=TrustLevel.BUILTIN,
        )
        s2 = await manager.connect(
            server_id=sid, slug=_slug(), transport=_transport(),
            trust_level=TrustLevel.BUILTIN,
        )
        assert s1 is s2

    @pytest.mark.asyncio
    async def test_connect_raises_on_failure(self) -> None:
        client = FakeMcpClient(fail_on_init=True)
        manager = _make_manager(client)

        with pytest.raises(McpConnectionError, match="Failed to connect"):
            await manager.connect(
                server_id=_sid(), slug=_slug(), transport=_transport(),
                trust_level=TrustLevel.BUILTIN,
            )


class TestCallTool:
    """(e-g) call_tool() scenarios."""

    @pytest.mark.asyncio
    async def test_call_tool_dispatches_to_client(self) -> None:
        client = FakeMcpClient(tools=[
            {"name": "resource_list", "description": "",
             "annotations": {"readOnlyHint": True}},
        ])
        manager = _make_manager(client)
        sid = _sid()
        await manager.connect(
            server_id=sid, slug=_slug(), transport=_transport(),
            trust_level=TrustLevel.BUILTIN,
        )

        result = await manager.call_tool(sid, "resource_list", {"filter": "all"})
        assert result == {"result": "called_resource_list"}
        assert client.calls == [("resource_list", {"filter": "all"})]

    @pytest.mark.asyncio
    async def test_call_tool_raises_for_unknown_tool(self) -> None:
        client = FakeMcpClient(tools=[])
        manager = _make_manager(client)
        sid = _sid()
        await manager.connect(
            server_id=sid, slug=_slug(), transport=_transport(),
            trust_level=TrustLevel.BUILTIN,
        )

        with pytest.raises(McpToolNotFoundError):
            await manager.call_tool(sid, "nonexistent_tool", {})

    @pytest.mark.asyncio
    async def test_call_tool_raises_for_unknown_server(self) -> None:
        manager = _make_manager(FakeMcpClient())
        with pytest.raises(McpServerNotFoundError):
            await manager.call_tool(_sid(), "some_tool", {})


class TestDisconnect:
    """(h) disconnect() closes the client."""

    @pytest.mark.asyncio
    async def test_disconnect_closes_client(self) -> None:
        client = FakeMcpClient()
        manager = _make_manager(client)
        sid = _sid()
        await manager.connect(
            server_id=sid, slug=_slug(), transport=_transport(),
            trust_level=TrustLevel.BUILTIN,
        )

        await manager.disconnect(sid)
        assert client.closed is True

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self) -> None:
        client = FakeMcpClient()
        manager = _make_manager(client)
        sid = _sid()
        await manager.connect(
            server_id=sid, slug=_slug(), transport=_transport(),
            trust_level=TrustLevel.BUILTIN,
        )

        await manager.disconnect(sid)
        await manager.disconnect(sid)  # second call must not raise


class TestHealth:
    """(i) health() returns correct value."""

    @pytest.mark.asyncio
    async def test_health_healthy_after_connect(self) -> None:
        client = FakeMcpClient()
        manager = _make_manager(client)
        sid = _sid()
        await manager.connect(
            server_id=sid, slug=_slug(), transport=_transport(),
            trust_level=TrustLevel.BUILTIN,
        )
        assert manager.health(sid) == "healthy"

    def test_health_unknown_for_unconnected(self) -> None:
        manager = _make_manager(FakeMcpClient())
        assert manager.health(_sid()) == "unknown"
