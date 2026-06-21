"""mcp/application/McpServerManager — owns N live MCP connections.

Application layer: orchestrates domain entities and the McpClientPort.
No I/O directly — all I/O delegated to the injected client factory.

Thread/task safety: each server_id maps to exactly one client; callers
must not call connect() concurrently for the same server_id.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from hermes.mcp.domain.entities import McpServer, McpTool
from hermes.mcp.domain.value_objects import McpServerId, ServerSlug, Transport, TrustLevel

from .errors import McpConnectionError, McpServerNotFoundError, McpToolNotFoundError
from .ports import McpClientPort

logger = logging.getLogger("hermes.mcp.server_manager")

ClientFactory = Callable[[Transport], McpClientPort]


class McpServerManager:
    """Owns N live MCP connections keyed by McpServerId.

    Args:
        client_factory: callable that returns an McpClientPort for a Transport.
                        Injected for testability (the real factory returns
                        StdioMcpClient; tests inject a fake).
    """

    def __init__(self, *, client_factory: ClientFactory) -> None:
        self._client_factory = client_factory
        self._servers: dict[str, McpServer] = {}   # server_id str → McpServer
        self._clients: dict[str, McpClientPort] = {}  # server_id str → client
        # Camino A: callback (server, loop) disparado al conectar, para registrar
        # las tools del MCP en el tools.registry GLOBAL de Nous (lo cablea __main__
        # con broker+consent). Así el agente las ve vía enabled_toolsets=None sin
        # depender del path per-ciclo de run_cycle._resolve_external_specs.
        self._on_connect = None

    async def connect(
        self,
        *,
        server_id: McpServerId,
        slug: ServerSlug,
        transport: Transport,
        trust_level: TrustLevel,
    ) -> McpServer:
        """Open a connection and populate the server's tool list.

        Idempotent: if a healthy connection already exists for server_id,
        returns the existing McpServer without reconnecting.

        Raises:
            McpConnectionError: if the transport cannot be established.
        """
        sid = str(server_id)
        if sid in self._servers and sid in self._clients:
            logger.info("hermes.mcp.manager.already_connected: server_id=%s", sid)
            return self._servers[sid]

        server = McpServer(
            server_id=server_id,
            slug=slug,
            transport=transport,
            trust_level=trust_level,
        )
        client = self._client_factory(transport)

        try:
            await client.initialize()
            raw_tools = await client.list_tools()
        except Exception as exc:
            server.mark_failed()
            logger.error(
                "hermes.mcp.manager.connect_failed: server_id=%s error=%s", sid, exc
            )
            raise McpConnectionError(
                f"Failed to connect to MCP server {slug!r}: {exc}"
            ) from exc

        tools = [_build_tool(t, slug, trust_level) for t in raw_tools]
        server.mark_healthy(tools)
        self._servers[sid] = server
        self._clients[sid] = client
        if self._on_connect is not None:
            try:
                import asyncio as _aio  # noqa: PLC0415
                self._on_connect(server, _aio.get_running_loop())
            except Exception as _oc_exc:  # noqa: BLE001
                logger.warning("hermes.mcp.manager.on_connect_failed: %s", _oc_exc)

        logger.info(
            "hermes.mcp.manager.connected: server_id=%s slug=%s tools=%d",
            sid, str(slug), len(tools),
        )
        return server

    async def list_tools(self, server_id: McpServerId) -> list[McpTool]:
        """Return cached tools for a connected server."""
        server = self._get_server(server_id)
        return list(server.tools)

    async def call_tool(
        self,
        server_id: McpServerId,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke a tool on the given server.

        Raises:
            McpServerNotFoundError: server_id not connected.
            McpToolNotFoundError: tool not in server's tool list.
            McpConnectionError: transport error during the call.
        """
        server = self._get_server(server_id)
        if server.get_tool(tool_name) is None:
            raise McpToolNotFoundError(
                f"Tool {tool_name!r} not found on server {server_id}"
            )

        client = self._clients[str(server_id)]
        return await client.call_tool(tool_name, args)

    async def disconnect(self, server_id: McpServerId) -> None:
        """Close the connection gracefully. Idempotent."""
        sid = str(server_id)
        client = self._clients.pop(sid, None)
        server = self._servers.pop(sid, None)
        if client is not None:
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hermes.mcp.manager.disconnect_error: server_id=%s error=%s", sid, exc
                )
        if server is not None:
            logger.info("hermes.mcp.manager.disconnected: server_id=%s", sid)

    def health(self, server_id: McpServerId) -> str:
        """Return health string for a server, or 'unknown' if not found."""
        server = self._servers.get(str(server_id))
        if server is None:
            return "unknown"
        return server.health.value

    def _get_server(self, server_id: McpServerId) -> McpServer:
        server = self._servers.get(str(server_id))
        if server is None:
            raise McpServerNotFoundError(
                f"No active MCP connection for server_id={server_id}"
            )
        return server


def _build_tool(raw: dict[str, Any], slug: ServerSlug, trust_level: TrustLevel) -> McpTool:
    """Parse a raw MCP tool descriptor into a McpTool entity."""
    annotations = raw.get("annotations") or {}
    return McpTool.build(
        name=str(raw.get("name", "")),
        description=str(raw.get("description", "")),
        slug=slug,
        trust_level=trust_level,
        read_only_hint=annotations.get("readOnlyHint"),
        destructive_hint=annotations.get("destructiveHint"),
    )
