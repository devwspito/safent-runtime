"""mcp/application/ports — McpClientPort Protocol.

Defined here (application layer) so that infrastructure adapters depend inward.
Domain layer never imports this; infrastructure implements it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class McpClientPort(Protocol):
    """Transport-neutral interface for a single MCP server connection.

    Implementations: StdioMcpClient (P1), HttpSseMcpClient (P4+).
    """

    async def initialize(self) -> None:
        """Open the transport and perform the MCP handshake.

        Raises:
            McpConnectionError: if the transport cannot be established.
        """
        ...

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return raw tool descriptors from the MCP server.

        Each descriptor is a dict with at least 'name' and 'description'.
        Additional keys (inputSchema, annotations) are passed through as-is
        and treated as untrusted metadata.

        Returns [] on empty server; never raises on empty list.
        """
        ...

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool and return the raw result as a dict.

        Raises:
            McpCallError: if the server returns an error.
            McpConnectionError: if the transport is down.
        """
        ...

    async def close(self) -> None:
        """Tear down the connection gracefully. Idempotent."""
        ...
