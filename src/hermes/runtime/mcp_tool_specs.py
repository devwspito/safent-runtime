"""Build ToolSpec list from connected MCP servers.

Mirrors composio_tool_specs.py in structure and security posture.

Rules:
  - Only tools from CONNECTED servers are built (zero specs when no servers
    are connected — this is correct and expected in P1).
  - READ tools (auto_executable=True per classify_mcp_tool) → READ_ONLY risk
    with a broker-dispatching handler. Every READ goes through broker.dispatch
    (consent + audit + kill-switch). NEVER calls McpServerManager directly.
  - WRITE tools (auto_executable=False) → WRITE_PROPOSAL, handler=None.
    Routes through proposal/HITL path in GovernedAIAgent._dispatch_external_write.
  - Classification is conservative: classify_mcp_tool defaults HIGH/not-auto.
  - Errors while listing tools for a server are logged and that server is
    skipped (fail-soft per server, not per run_cycle).

Tool naming: mcp__<slug>__<tool_name> — the qualified name that:
  1. McpCapabilityRegistry.resolve() parses to find the server+tool.
  2. McpSurfaceAdapter.replay() uses (via server_id="" + bare_tool_name in payload)
     to call the right server through McpServerManager.

Security invariant: every MCP tool call (READ and WRITE) passes through
broker.dispatch EXACTLY ONCE. McpServerManager is never called from this module.

Called from __main__._tools_source after native OS tools + Composio tools.
Included only when an McpServerManager instance is available.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from hermes.capabilities.domain.ports import RiskLevel
from hermes.domain.tool_spec import ToolRisk, ToolSpec

if TYPE_CHECKING:
    from hermes.capabilities.domain.ports import CapabilityBrokerPort, ConsentContext
    from hermes.mcp.application.mcp_server_manager import McpServerManager

logger = logging.getLogger("hermes.runtime.mcp_tools")


def _mcp_risk_to_tool_risk(auto_executable: bool) -> ToolRisk:
    """Map McpToolClassification.auto_executable to ToolRisk.

    auto_executable=True (RiskLevel.LOW) → READ_ONLY (handler present, broker-dispatched).
    auto_executable=False (RiskLevel.HIGH) → WRITE_PROPOSAL (handler=None, HITL gate).
    """
    return ToolRisk.READ_ONLY if auto_executable else ToolRisk.WRITE_PROPOSAL


async def build_mcp_tool_specs(
    server_manager: "McpServerManager",
    *,
    broker: "CapabilityBrokerPort",
    consent_context: "ConsentContext",
) -> tuple[ToolSpec, ...]:
    """Build ToolSpec instances for all tools of all connected MCP servers.

    Steps:
      1. Iterate over connected servers in server_manager.
      2. For each server, list its tools (cached in-memory by the manager).
      3. Classify each tool; build ToolSpec with qualified name.

    Args:
        server_manager:  McpServerManager — owns live connections; tool lists
                         are cached from connect() time, so list_tools() is O(1).
        broker:          CapabilityBroker — routes READ actions through the
                         full gate (consent + audit + kill-switch).
        consent_context: ConsentContext for the current agent cycle.

    Returns an empty tuple when no servers are connected (correct in P1 — MCP
    server catalog/connect UX is P3). Logs at INFO level so operators know
    why no MCP tools appear.

    Errors per tool are fail-soft: one bad tool descriptor does not block the
    rest of the server's tools.
    """
    from hermes.mcp.domain.value_objects import McpServerId  # noqa: PLC0415
    from hermes.runtime.mcp_broker_handler import make_mcp_broker_read_handler  # noqa: PLC0415

    active_servers = list(server_manager._servers.values())

    if not active_servers:
        logger.info("hermes.mcp_tools.no_connected_servers — zero MCP tool specs built")
        return ()

    specs: list[ToolSpec] = []

    for server in active_servers:
        slug_str = str(server.slug)
        server_id = server.server_id
        tools = server.tools

        for tool in tools:
            try:
                spec = _mcp_tool_to_spec(
                    qualified_name=tool.qualified_name,
                    bare_tool_name=tool.name,
                    description=tool.description,
                    input_schema=None,  # schema is from tool entity (no raw schema here)
                    auto_executable=tool.auto_executable,
                    broker=broker,
                    consent_context=consent_context,
                )
                specs.append(spec)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hermes.mcp_tools.spec_build_failed: server=%s tool=%s error=%s",
                    slug_str,
                    tool.name,
                    exc,
                )

        logger.debug(
            "hermes.mcp_tools.server_tools_added: server=%s tool_count=%d",
            slug_str,
            len(tools),
        )

    logger.info(
        "hermes.mcp_tools.built",
        extra={
            "server_count": len(active_servers),
            "tool_count": len(specs),
        },
    )
    return tuple(specs)


def _mcp_tool_to_spec(
    *,
    qualified_name: str,
    bare_tool_name: str,
    description: str,
    input_schema: dict[str, Any] | None,
    auto_executable: bool,
    broker: "CapabilityBrokerPort",
    consent_context: "ConsentContext",
) -> ToolSpec:
    """Convert a McpTool entity to a ToolSpec.

    READ tools (auto_executable=True) receive a broker-dispatching handler.
    WRITE tools (auto_executable=False) receive handler=None — they route via
    GovernedAIAgent._dispatch_external_write → broker.dispatch.
    """
    risk = _mcp_risk_to_tool_risk(auto_executable)
    schema = input_schema or {"type": "object", "properties": {}}

    handler = None
    if risk == ToolRisk.READ_ONLY:
        from hermes.runtime.mcp_broker_handler import make_mcp_broker_read_handler  # noqa: PLC0415
        handler = make_mcp_broker_read_handler(
            qualified_name=qualified_name,
            bare_tool_name=bare_tool_name,
            broker=broker,
            consent_context=consent_context,
        )

    return ToolSpec(
        name=qualified_name,
        description=description or f"MCP tool {qualified_name}",
        parameters_schema=schema,
        risk=risk,
        entity_type="mcp",
        handler=handler,
        tags=("mcp",),
    )
