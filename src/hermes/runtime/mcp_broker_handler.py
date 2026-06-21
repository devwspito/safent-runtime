"""Broker-dispatching handler for MCP READ tool calls.

Mirrors composio_broker_handler.py in structure. Intentionally separate from
mcp_tool_specs to keep the import graph clean — this module has zero MCP SDK
or hermes-agent dependencies.

Security guarantees (identical to Composio READ path):
  - Kill-switch (CTRL-12): aborted if agent_state.is_paused().
  - Consent gate (CTRL-2/13): operator_id None → fail-closed.
  - Audit (CTRL-9): PROPOSAL_EXECUTED entry signed and persisted.
  - Taint propagation (CTRL-5): handled by CapturingToolHost via the
    "mcp" tag on the ToolSpec (this module doesn't duplicate that).

Every MCP READ is dispatched through broker.dispatch EXACTLY ONCE.
The MCP server is never called directly from this handler.

Capa: runtime (wires domain ports together, no framework, no I/O directa).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from hermes.capabilities.domain.ports import CapabilityBrokerPort, ConsentContext

logger = logging.getLogger("hermes.runtime.mcp_broker_handler")


def make_mcp_broker_read_handler(
    *,
    qualified_name: str,
    bare_tool_name: str,
    broker: "CapabilityBrokerPort",
    consent_context: "ConsentContext",
) -> Any:
    """Return an async callable that routes an MCP READ through broker.dispatch.

    Args:
        qualified_name: full mcp__<slug>__<tool> name used as tool_name in the
                        proposal so McpCapabilityRegistry can resolve it.
        bare_tool_name: just the tool portion (e.g. "list_files") — placed in
                        parameters["tool_name"] so McpSurfaceAdapter can call
                        manager.call_tool(server_id, tool_name, args).
        broker:          CapabilityBroker — applies full gate chain (CTRL-1..14).
        consent_context: ConsentContext for the current agent cycle.

    The proposal passed to broker.dispatch has:
        tool_name  = qualified_name      (matches McpCapabilityRegistry.resolve)
        entity_type = "mcp"
        parameters = {"server_id": "", "tool_name": bare_tool_name, "args": ...}
          → server_id="" signals McpSurfaceAdapter to resolve from qualified_name.
    """

    async def _broker_handler(params: dict[str, Any]) -> dict[str, Any]:
        from hermes.domain.proposal import ToolCallProposal  # noqa: PLC0415
        from hermes.capabilities.domain.ports import ExecutionStatus  # noqa: PLC0415

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name=qualified_name,
            tenant_id=consent_context.tenant_id,
            entity_id="mcp_read",
            entity_type="mcp",
            parameters={
                "server_id": "",
                "qualified_name": qualified_name,
                "tool_name": bare_tool_name,
                "args": dict(params),
            },
            justification=f"MCP READ: {qualified_name}",
        )
        outcome = await broker.dispatch(proposal, consent_context)

        if outcome.status is ExecutionStatus.EXECUTED:
            return outcome.result or {}

        logger.warning(
            "hermes.mcp_broker_handler.read_rejected: tool=%s status=%s error=%s",
            qualified_name,
            outcome.status,
            outcome.error,
        )
        return {
            "error": f"mcp_read_blocked: {outcome.status}",
            "detail": outcome.error or "",
        }

    return _broker_handler
