"""Agents REST API — full D-Bus surface for agent roster management.

Endpoints:
  GET    /api/v1/agents                              list all agents
  POST   /api/v1/agents                              create a new agent
  PUT    /api/v1/agents/{id}                         update agent
  PATCH  /api/v1/agents/{id}                         update agent (same verb, web sends PATCH)
  DELETE /api/v1/agents/{id}                         delete agent
  POST   /api/v1/agents/{id}/activate                set active agent
  GET    /api/v1/agents/active                       get active agent id
  GET    /api/v1/agents/{id}/capabilities            list capabilities bound to agent
  POST   /api/v1/agents/{id}/capabilities            bind a capability to agent
  DELETE /api/v1/agents/{id}/capabilities/{cap_id}  unbind a capability (kind via query param)
  GET    /api/v1/agents/{id}/composio               list composio connections for agent
  POST   /api/v1/agents/{id}/composio               bind composio connection to agent
  DELETE /api/v1/agents/{id}/composio/{conn_id}     unbind composio connection from agent
  GET    /api/v1/composio/connections               list all composio connections

D-Bus arg order (verified from hermes_backend.py / dbus_fast_runtime_client.py):
  list_agent_capabilities(agent_id)
  bind_capability_to_agent(agent_id, capability_kind, capability_id, capability_version)
  unbind_capability_from_agent(agent_id, capability_kind, capability_id)
  list_composio_connections()
  list_agent_composio_connections(agent_id)                 → list[str] (connection_ids)
  bind_composio_connection_to_agent(agent_id, connection_id, toolkit_slug)
  unbind_composio_connection_from_agent(agent_id, connection_id)

Security:
  - Mutators carry a signed OperatorToken via DbusRuntimeProxy.call_mutator().
  - fail-soft for GETs; fail-hard 503 for mutators (CTRL-P1-11).
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes.shell_server.cowork.agents_api")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _normalize_agent_dict(d: dict) -> dict:
    """Ensure the agent dict carries both 'agent_id' and 'id' so the
    React frontend (which reads Agent.id) and the daemon protocol
    (which emits 'agent_id') both work without a client-side shim.
    """
    if "agent_id" in d and "id" not in d:
        return {**d, "id": d["agent_id"]}
    return d


# ------------------------------------------------------------------
# Pydantic schemas
# ------------------------------------------------------------------


class AgentDraft(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="", max_length=512)
    # Tone/register instruction forwarded to the prompt builder.
    register: str = Field(default="", max_length=512)
    primary_mission: str = Field(default="", max_length=2000)
    instructions: str = Field(default="", max_length=8000)
    language: str = Field(default="es-ES", max_length=20)
    color: str = Field(default="#6366f1", max_length=30)
    golden_rules: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    autonomy_level: str = Field(default="balanced")
    is_default: bool = False
    # Optional department slug; null → rendered in "mis-agentes" bucket.
    department: str | None = Field(default=None, max_length=64)


class BindCapabilityRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=64, description="'skill' or 'mcp'")
    capability_id: str = Field(min_length=1, max_length=256)
    capability_version: str = Field(default="", max_length=64)


class BindComposioConnectionRequest(BaseModel):
    connection_id: str = Field(min_length=1, max_length=256)
    toolkit_slug: str = Field(default="", max_length=128)


# ------------------------------------------------------------------
# Router factory
# ------------------------------------------------------------------


def create_agents_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

    # ------------------------------------------------------------------
    # Roster reads
    # ------------------------------------------------------------------

    @router.get("")
    async def list_agents(request: Request) -> list[dict]:
        """List all agents in the roster. Fail-soft: [] when daemon unavailable."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_list("list_agents")
        except AgentUnavailable as exc:
            logger.warning("hermes.agents.list_unavailable", extra={"reason": str(exc)})
            return []

    @router.get("/active")
    async def get_active_agent(request: Request) -> dict:
        """Return the id of the currently active agent. Fail-soft: empty string."""
        proxy = request.app.state.dbus_proxy
        active_id = await proxy.call_str("get_active_agent")
        return {"active_agent_id": active_id}

    # ------------------------------------------------------------------
    # Roster mutators
    # ------------------------------------------------------------------

    # The daemon's draft validator only accepts these keys (Fix-10 allow-list);
    # `is_default` is NOT accepted (it's seeded, never client-set) and any extra
    # key makes the daemon reject the whole draft → 503. Filter to the allow-list.
    _DRAFT_KEYS = {
        "name", "role", "register", "primary_mission", "instructions",
        "color", "language", "golden_rules", "forbidden_phrases", "autonomy_level",
        "department",
    }

    def _clean_draft(body: AgentDraft) -> dict:
        return {k: v for k, v in body.model_dump().items() if k in _DRAFT_KEYS}

    @router.post("", status_code=201)
    async def create_agent(request: Request, body: AgentDraft) -> dict:
        """Create a new agent."""
        proxy = request.app.state.dbus_proxy
        try:
            d = await proxy.call_mutator("create_agent", json.dumps(_clean_draft(body)))
            return _normalize_agent_dict(d)
        except AgentUnavailable as exc:
            _raise_503(exc, "create_agent")

    @router.put("/{agent_id}")
    @router.patch("/{agent_id}")
    async def update_agent(request: Request, agent_id: str, body: AgentDraft) -> dict:
        """Update an existing agent (PUT and PATCH are equivalent here)."""
        proxy = request.app.state.dbus_proxy
        try:
            d = await proxy.call_mutator("update_agent", agent_id, json.dumps(_clean_draft(body)))
            return _normalize_agent_dict(d)
        except AgentUnavailable as exc:
            _raise_503(exc, "update_agent")

    @router.delete("/{agent_id}", status_code=204)
    async def delete_agent(request: Request, agent_id: str) -> None:
        """Delete an agent from the roster."""
        proxy = request.app.state.dbus_proxy
        try:
            await proxy.call_bool("delete_agent", agent_id)
        except AgentUnavailable as exc:
            _raise_503(exc, "delete_agent")

    @router.post("/{agent_id}/activate")
    async def activate_agent(request: Request, agent_id: str) -> dict:
        """Set the active agent."""
        proxy = request.app.state.dbus_proxy
        try:
            await proxy.call_bool("set_active_agent", agent_id)
            return {"ok": True, "active_agent_id": agent_id}
        except AgentUnavailable as exc:
            _raise_503(exc, "set_active_agent")

    # ------------------------------------------------------------------
    # Per-agent capabilities
    # D-Bus positional order: (agent_id) / (agent_id, kind, cap_id, version)
    #                         / (agent_id, kind, cap_id)
    # ------------------------------------------------------------------

    @router.get("/{agent_id}/capabilities")
    async def list_agent_capabilities(request: Request, agent_id: str) -> list[dict]:
        """List capabilities bound to an agent. Fail-soft: []."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_list("list_agent_capabilities", agent_id)
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.agents.capabilities.list_unavailable",
                extra={"agent_id": agent_id, "reason": str(exc)},
            )
            return []

    @router.post("/{agent_id}/capabilities", status_code=201)
    async def bind_capability_to_agent(
        request: Request, agent_id: str, body: BindCapabilityRequest
    ) -> dict:
        """Bind a capability (skill or MCP server) to an agent."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_mutator(
                "bind_capability_to_agent",
                agent_id,
                body.kind,
                body.capability_id,
                body.capability_version,
            )
        except AgentUnavailable as exc:
            _raise_503(exc, "bind_capability_to_agent")

    @router.delete("/{agent_id}/capabilities/{capability_id}", status_code=204)
    async def unbind_capability_from_agent(
        request: Request,
        agent_id: str,
        capability_id: str,
        kind: str = Query(description="Capability kind: 'skill' or 'mcp'"),
    ) -> None:
        """Unbind a capability from an agent."""
        proxy = request.app.state.dbus_proxy
        try:
            await proxy.call_bool(
                "unbind_capability_from_agent", agent_id, kind, capability_id
            )
        except AgentUnavailable as exc:
            _raise_503(exc, "unbind_capability_from_agent")

    # ------------------------------------------------------------------
    # Per-agent Composio connections
    # D-Bus positional order:
    #   list_agent_composio_connections(agent_id) → list[str]
    #   bind_composio_connection_to_agent(agent_id, connection_id, toolkit_slug)
    #   unbind_composio_connection_from_agent(agent_id, connection_id)
    # ------------------------------------------------------------------

    @router.get("/{agent_id}/composio")
    async def list_agent_composio_connections(
        request: Request, agent_id: str
    ) -> list[str]:
        """List Composio connection ids bound to the agent. Fail-soft: []."""
        proxy = request.app.state.dbus_proxy
        try:
            raw = await proxy.call_list("list_agent_composio_connections", agent_id)
            # Daemon returns list[str]; call_list wraps list transparently.
            return [str(c) for c in raw]
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.agents.composio.list_unavailable",
                extra={"agent_id": agent_id, "reason": str(exc)},
            )
            return []

    @router.post("/{agent_id}/composio", status_code=201)
    async def bind_composio_connection_to_agent(
        request: Request, agent_id: str, body: BindComposioConnectionRequest
    ) -> dict:
        """Bind a Composio connected account to an agent."""
        proxy = request.app.state.dbus_proxy
        try:
            result = await proxy.call_bool(
                "bind_composio_connection_to_agent",
                agent_id,
                body.connection_id,
                body.toolkit_slug,
            )
            return {"ok": bool(result)}
        except AgentUnavailable as exc:
            _raise_503(exc, "bind_composio_connection_to_agent")

    @router.delete("/{agent_id}/composio/{connection_id}", status_code=204)
    async def unbind_composio_connection_from_agent(
        request: Request, agent_id: str, connection_id: str
    ) -> None:
        """Unbind a Composio connection from an agent."""
        proxy = request.app.state.dbus_proxy
        try:
            await proxy.call_bool(
                "unbind_composio_connection_from_agent", agent_id, connection_id
            )
        except AgentUnavailable as exc:
            _raise_503(exc, "unbind_composio_connection_from_agent")

    return router


# ------------------------------------------------------------------
# Composio global router (separate prefix /api/v1/composio)
# ------------------------------------------------------------------


def create_composio_router() -> APIRouter:
    """Router for /api/v1/composio — global Composio connection listing."""
    router = APIRouter(prefix="/api/v1/composio", tags=["composio"])

    @router.get("/connections")
    async def list_composio_connections(request: Request) -> list[dict]:
        """List all Composio connected accounts for this entity. Fail-soft: []."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_list("list_composio_connections")
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.composio.connections.list_unavailable",
                extra={"reason": str(exc)},
            )
            return []

    return router


def _raise_503(exc: AgentUnavailable, operation: str) -> None:
    logger.warning(
        "hermes.agents.mutator_unavailable",
        extra={"operation": operation, "reason": str(exc)},
    )
    raise HTTPException(
        status_code=503,
        detail={
            "code": "agent_unavailable",
            "message": "El agente no está disponible. Comprueba que hermes-runtime está activo.",
        },
    ) from exc
