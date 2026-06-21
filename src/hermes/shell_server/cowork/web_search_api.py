"""Web search backend keys REST API — D-Bus surface for Brave/Tavily/Exa.

Endpoints:
  GET  /api/v1/web-search/status     which backends have a key (read-only)
  POST /api/v1/web-search/key        set a backend's API key (Brave/Tavily/Exa)

Setting a key takes effect immediately (the daemon injects it into os.environ
live AND persists it to HERMES_HOME/.env). web_search then prefers that backend,
with the keyless `ddgs` (DuckDuckGo) as fallback.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes.shell_server.cowork.web_search_api")


class SetWebSearchKeyRequest(BaseModel):
    provider: str = Field(min_length=1, description="brave | tavily | exa")
    api_key: str = Field(min_length=1, max_length=400)


def create_web_search_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/web-search", tags=["web-search"])

    @router.get("/status")
    async def web_search_status(request: Request) -> dict:
        """Which web-search backends have an API key configured. Fail-soft."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_dict("get_web_search_status")
        except AgentUnavailable as exc:
            logger.warning("hermes.web_search.status_unavailable", extra={"reason": str(exc)})
            return {"brave": False, "tavily": False, "exa": False, "ddgs_fallback": True}

    @router.post("/key")
    async def set_web_search_key(request: Request, body: SetWebSearchKeyRequest) -> dict:
        """Set a web-search backend API key (takes effect immediately)."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_mutator("set_web_search_api_key", body.provider, body.api_key)
        except AgentUnavailable as exc:
            logger.warning("hermes.web_search.mutator_unavailable", extra={"reason": str(exc)})
            raise HTTPException(
                status_code=503,
                detail={"code": "agent_unavailable", "message": "El agente no está disponible."},
            ) from exc

    return router
