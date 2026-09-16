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


_REF_DEPTH_LIMIT = 12


def inline_local_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Return ``schema`` with local ``$ref``s (``#/$defs/X``, ``#/definitions/X``)
    expanded in place and the definition tables dropped.

    Pydantic/FastMCP publish nested argument models through ``$defs`` + ``$ref``.
    LLM function-calling surfaces do not reliably render those, so the model sees
    a nested field as a bare ``object`` and guesses its keys — observed 2026-09-14
    with safent-ads ``propose_campaign_draft`` (``changes`` is a strict, closed
    model): nine "extra inputs are not permitted" errors and an invented
    ``platform`` value. Recursive refs stop at a depth limit and stay as-is.
    """
    defs: dict[str, Any] = {}
    for table in ("$defs", "definitions"):
        found = schema.get(table)
        if isinstance(found, dict):
            defs.update(found)
    if not defs:
        return schema

    def _resolve(node: Any, depth: int) -> Any:
        if isinstance(node, list):
            return [_resolve(item, depth) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            name = ref.rsplit("/", 1)[-1]
            target = defs.get(name)
            if isinstance(target, dict) and depth < _REF_DEPTH_LIMIT:
                merged = {k: v for k, v in node.items() if k != "$ref"}
                merged.update(_resolve(target, depth + 1))
                return merged
            return node
        return {
            key: _resolve(value, depth)
            for key, value in node.items()
            if key not in ("$defs", "definitions")
        }

    return _resolve(schema, 0)


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
                    input_schema=tool.input_schema,
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
    schema = inline_local_refs(input_schema or {"type": "object", "properties": {}})

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
