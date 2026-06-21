"""mcp/infrastructure/McpSurfaceAdapter — SurfaceAdapterPort for MCP_CALL.

Dispatches broker-approved MCP tool calls through McpServerManager.
Mirrors composio_surface_adapter.py in structure.

Security guarantees:
  - Execution ONLY happens after the full broker gate chain passes (CTRL-1..14).
  - surface_kind mismatch → REJECTED_BY_POLICY (fail-closed).
  - Missing server_id or tool_name → REJECTED_BY_POLICY.
  - Bounded timeout (fail-closed on slow/hung server).
  - Result tagged is_external_content=True so the orchestrator propagates
    taint to the ConsentContext (CTRL-5).

Capa: infrastructure (adapts McpServerManager to SurfaceAdapterPort).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.mcp.application.errors import McpCallError, McpConnectionError, McpServerNotFoundError
from hermes.mcp.application.mcp_server_manager import McpServerManager
from hermes.mcp.domain.value_objects import McpServerId

logger = logging.getLogger("hermes.mcp.surface_adapter")

_MCP_EXEC_TIMEOUT_S: float = 25.0


class McpSurfaceAdapter:
    """SurfaceAdapterPort for MCP tool calls (SurfaceKind.MCP_CALL).

    Args:
        server_manager: owns live connections; used to dispatch the call.
    """

    def __init__(self, *, server_manager: McpServerManager) -> None:
        self._manager = server_manager

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.MCP_CALL

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> CapturedAction:
        """capture() is not used for MCP calls — only replay() is relevant."""
        raise NotImplementedError(
            "McpSurfaceAdapter.capture is not used. "
            "MCP actions are replayed via replay() from the broker."
        )

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        """Execute the MCP tool call defined in action.payload.

        Expected payload:
            server_id: str  — McpServerId string.
            tool_name: str  — bare tool name (not qualified).
            args:      dict — tool arguments.

        Fail-closed:
            - surface_kind != MCP_CALL → REJECTED_BY_POLICY.
            - Missing server_id or tool_name → REJECTED_BY_POLICY.
            - Server not connected → REJECTED_BY_POLICY.
            - Tool not found → REJECTED_BY_POLICY.
            - Network / timeout error → EXECUTED_FAILED.
        """
        if action.surface_kind != SurfaceKind.MCP_CALL:
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error=(
                    f"surface_kind mismatch in McpSurfaceAdapter: "
                    f"expected MCP_CALL, got {action.surface_kind!r}"
                ),
            )

        server_id_str = action.payload.get("server_id")
        tool_name = action.payload.get("tool_name")
        if not tool_name:
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error="McpSurfaceAdapter: tool_name missing from payload — fail-closed",
            )

        # When server_id is empty, resolve the server from the qualified_name
        # (mcp__<slug>__<tool>) embedded in the payload. This is the standard
        # path for tool calls built by build_mcp_tool_specs / _shape_external_parameters.
        if not server_id_str:
            qualified_name = action.payload.get("qualified_name", "")
            server_id_str = self._resolve_server_id_from_qualified_name(qualified_name)
            if server_id_str is None:
                return ReplayOutcome(
                    action_id=action.action_id,
                    status=ReplayStatus.REJECTED_BY_POLICY,
                    error=(
                        f"McpSurfaceAdapter: server_id empty and qualified_name "
                        f"{qualified_name!r} did not resolve to a connected server — fail-closed"
                    ),
                )

        args = dict(action.payload.get("args") or {})

        return await self._execute(action.action_id, server_id_str, tool_name, args)

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        """Canonical serialization for HMAC audit (CTRL-9)."""
        canonical = {
            "surface_kind": action.surface_kind.value,
            "server_id": action.payload.get("server_id", ""),
            "tool_name": action.payload.get("tool_name", ""),
            "intent_desc": action.intent_desc,
        }
        return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _resolve_server_id_from_qualified_name(
        self,
        qualified_name: str,
    ) -> str | None:
        """Resolve a server_id string from mcp__<slug>__<tool> by matching slug.

        Returns the server_id UUID string for the first active server whose
        slug matches the slug parsed from qualified_name.  Returns None when
        qualified_name is malformed or no connected server matches the slug.
        """
        parts = qualified_name.split("__")
        if len(parts) != 3 or not parts[1]:
            logger.debug(
                "hermes.mcp.adapter.resolve_by_slug.malformed: qualified_name=%r",
                qualified_name,
            )
            return None
        slug_str = parts[1]
        for sid, server in self._manager._servers.items():
            if str(server.slug) == slug_str:
                return sid
        logger.debug(
            "hermes.mcp.adapter.resolve_by_slug.not_found: slug=%s",
            slug_str,
        )
        return None

    async def _execute(
        self,
        action_id: UUID,
        server_id_str: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> ReplayOutcome:
        """Call the MCP server with a bounded timeout."""
        import asyncio  # noqa: PLC0415

        try:
            server_id = McpServerId.from_str(server_id_str)
        except ValueError as exc:
            return ReplayOutcome(
                action_id=action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error=f"McpSurfaceAdapter: invalid server_id {server_id_str!r}: {exc}",
            )

        try:
            # asyncio.timeout (CM, MISMA task), NO wait_for: el call_tool atraviesa
            # los task-groups/cancel-scopes de anyio del cliente MCP; wait_for los
            # cruza de task y los rompe (mismo bug que el connect). Ver
            # stdio_mcp_client.initialize.
            async with asyncio.timeout(_MCP_EXEC_TIMEOUT_S):
                result = await self._manager.call_tool(server_id, tool_name, args)
        except asyncio.TimeoutError:
            logger.error(
                "hermes.mcp.adapter.timeout: tool_name=%s server_id=%s",
                tool_name, server_id_str,
            )
            return ReplayOutcome(
                action_id=action_id,
                status=ReplayStatus.EXECUTED_FAILED,
                error=f"McpSurfaceAdapter: timeout executing {tool_name!r}",
            )
        except (McpServerNotFoundError, KeyError):
            return ReplayOutcome(
                action_id=action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error=f"McpSurfaceAdapter: server_id={server_id_str!r} not connected — fail-closed",
            )
        except McpCallError as exc:
            logger.warning(
                "hermes.mcp.adapter.call_error: tool_name=%s error=%s",
                tool_name, str(exc),
            )
            return ReplayOutcome(
                action_id=action_id,
                status=ReplayStatus.EXECUTED_FAILED,
                error=f"McpCallError: {exc}",
            )
        except McpConnectionError as exc:
            logger.error(
                "hermes.mcp.adapter.connection_error: tool_name=%s error=%s",
                tool_name, str(exc),
            )
            return ReplayOutcome(
                action_id=action_id,
                status=ReplayStatus.EXECUTED_FAILED,
                error=f"McpConnectionError: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.mcp.adapter.unexpected: tool_name=%s error=%s",
                tool_name, str(exc),
            )
            return ReplayOutcome(
                action_id=action_id,
                status=ReplayStatus.EXECUTED_FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

        logger.info(
            "hermes.mcp.adapter.executed: tool_name=%s server_id=%s",
            tool_name, server_id_str,
        )
        result_dict = result if isinstance(result, dict) else {"data": result}
        result_dict["is_external_content"] = True   # CTRL-5 taint signal
        return ReplayOutcome(
            action_id=action_id,
            status=ReplayStatus.EXECUTED_OK,
            result=result_dict,
        )
