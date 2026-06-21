"""mcp/application/errors — named exception types for the MCP bounded context."""

from __future__ import annotations


class McpConnectionError(RuntimeError):
    """Transport cannot be established or was lost."""


class McpCallError(RuntimeError):
    """The MCP server returned a protocol-level error for a tool call."""


class McpServerNotFoundError(KeyError):
    """No active connection for the requested server_id."""


class McpToolNotFoundError(KeyError):
    """The tool is not exposed by the target server."""
