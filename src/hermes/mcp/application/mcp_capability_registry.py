"""mcp/application/McpCapabilityRegistry — dynamic CapabilityRegistryPort decorator.

Resolves `mcp__<slug>__<tool>` qualified names to CapabilityBinding.
Delegates all other names to the inner (static) registry.
Unknown / disabled server → None (broker fail-closes).

Mirrors composio_capability_registry.py in structure.

Capa: application (combina domain ports sin I/O directa).
"""

from __future__ import annotations

import logging

from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.domain.ports import (
    CapabilityBinding,
    CapabilityRegistryPort,
    RiskLevel,
)

from .mcp_server_manager import McpServerManager

logger = logging.getLogger("hermes.mcp.capability_registry")

_MCP_PREFIX = "mcp__"


class McpCapabilityRegistry:
    """CapabilityRegistryPort that resolves mcp__<slug>__<tool> names.

    Args:
        static_registry:  inner registry (checked first).
        server_manager:   owns live connections; used to look up tools.
    """

    def __init__(
        self,
        *,
        static_registry: CapabilityRegistryPort,
        server_manager: McpServerManager,
    ) -> None:
        self._static = static_registry
        self._manager = server_manager

    def resolve(self, tool_name: str) -> CapabilityBinding | None:
        """Resolve tool_name → CapabilityBinding.

        1. Delegate to static registry first (it wins on collision).
        2. If name looks like mcp__<slug>__<tool>, look up in the manager.
        3. Unknown / not-connected server → None (broker fail-closes).
        """
        static = self._static.resolve(tool_name)
        if static is not None:
            return static

        if not _is_mcp_qualified_name(tool_name):
            return None

        slug_str, bare_tool = _parse_qualified_name(tool_name)
        if slug_str is None or bare_tool is None:
            return None

        tool = self._find_tool(slug_str, bare_tool)
        if tool is None:
            logger.debug(
                "hermes.mcp.registry.tool_not_found: qualified_name=%s", tool_name
            )
            return None

        return CapabilityBinding(
            tool_name=tool_name,
            surface_kind=SurfaceKind.MCP_CALL,
            required_capability=None,
            risk=tool.risk,
            auto_executable=tool.auto_executable,
            executor="mcp",
        )

    def _find_tool(self, slug_str: str, bare_tool: str):  # type: ignore[return]
        """Search all active servers for a matching slug+tool."""
        from hermes.mcp.domain.entities import McpTool  # noqa: PLC0415

        for server in self._manager._servers.values():
            if str(server.slug) == slug_str:
                return server.get_tool(bare_tool)
        return None


def _is_mcp_qualified_name(name: str) -> bool:
    """True if name starts with 'mcp__' and has the three-part structure."""
    if not name.startswith(_MCP_PREFIX):
        return False
    parts = name.split("__")
    return len(parts) == 3


def _parse_qualified_name(name: str) -> tuple[str | None, str | None]:
    """Split mcp__<slug>__<tool> → (slug, tool). Returns (None, None) on error."""
    parts = name.split("__")
    if len(parts) != 3:
        return None, None
    _, slug_str, bare_tool = parts
    if not slug_str or not bare_tool:
        return None, None
    return slug_str, bare_tool
