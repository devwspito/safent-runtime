"""MCP servers REST API — D-Bus surface for MCP server management.

Endpoints:
  GET    /api/v1/mcp            list configured MCP servers
  POST   /api/v1/mcp            add a new MCP server
  DELETE /api/v1/mcp/{id}       remove an MCP server

Security:
  - Mutators carry a signed OperatorToken (DbusRuntimeProxy.call_mutator).
  - fail-soft for GET; fail-hard 503 for mutators (CTRL-P1-11).
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes.shell_server.cowork.mcp_api")


# ------------------------------------------------------------------
# Pydantic schemas
# ------------------------------------------------------------------


class AddMcpServerRequest(BaseModel):
    # Mirrors the daemon's add_mcp_server draft 1:1 (server_id/label/argv/env) so
    # the frontend → shell-server → daemon contract is a single shape. argv[0]
    # must be an allowed runner (npx/uvx/node/python3); the daemon validates.
    server_id: str = Field(min_length=1, max_length=120)
    label: str | None = Field(default=None)
    argv: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict, description="BYOK env vars")


# ------------------------------------------------------------------
# Router factory
# ------------------------------------------------------------------


def create_mcp_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])

    @router.get("")
    async def list_mcp_servers(request: Request) -> list[dict]:
        """List configured MCP servers with status and tool_count.

        Fail-soft: returns [] when daemon unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_list("list_mcp_servers")
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.mcp.list_unavailable",
                extra={"reason": str(exc)},
            )
            return []

    @router.post("", status_code=201)
    async def add_mcp_server(request: Request, body: AddMcpServerRequest) -> dict:
        """Register a new MCP server (local command or remote URL)."""
        proxy = request.app.state.dbus_proxy
        draft = {
            "server_id": body.server_id,
            "label": body.label or body.server_id,
            "argv": body.argv,
            "env": body.env,
        }
        try:
            return await proxy.call_mutator("add_mcp_server", json.dumps(draft))
        except AgentUnavailable as exc:
            _raise_503(exc, "add_mcp_server")

    @router.delete("/{server_id}", status_code=204)
    async def remove_mcp_server(request: Request, server_id: str) -> None:
        """Remove a registered MCP server."""
        proxy = request.app.state.dbus_proxy
        try:
            await proxy.call_mutator("remove_mcp_server", server_id)
        except AgentUnavailable as exc:
            _raise_503(exc, "remove_mcp_server")

    @router.get("/registry")
    async def search_mcp_registry(request: Request, q: str = "", limit: int = 30) -> list[dict]:
        """Search the official MCP registry (registry.modelcontextprotocol.io).

        Proxies the daemon's search_mcp_registry, which returns entries already
        normalised to the add_mcp_server shape (server_id/label/argv/...). Parity
        with the native SO (McpApp.qml "registry" source). Fail-soft: [] on error.
        """
        if not q or len(q.strip()) < 2:
            return []
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_list("search_mcp_registry", q.strip(), int(limit))
        except AgentUnavailable as exc:
            logger.warning("hermes.mcp.registry_unavailable", extra={"reason": str(exc)})
            return []

    return router


def _raise_503(exc: AgentUnavailable, operation: str) -> None:
    logger.warning(
        "hermes.mcp.mutator_unavailable",
        extra={"operation": operation, "reason": str(exc)},
    )
    raise HTTPException(
        status_code=503,
        detail={
            "code": "agent_unavailable",
            "message": "El agente no está disponible. Comprueba que hermes-runtime está activo.",
        },
    ) from exc
