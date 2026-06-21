"""Memory REST API — D-Bus surface for agent memory retrieval.

Endpoints:
  GET  /api/v1/memory         list recent memory entries
  GET  /api/v1/memory/search  semantic search (?q=query)

Both endpoints are read-only and fail-soft (return [] on daemon unavailable).
Memory entries are informational only — no write surface exposed here
(writes go through the agent's own learning loop).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request

from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes.shell_server.cowork.memory_api")


def create_memory_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

    @router.get("")
    async def list_memory(
        request: Request,
        limit: int = Query(50, le=500),
    ) -> list[dict]:
        """List recent agent memory entries.

        Returns [{entry_index, target, content, created_at}].
        Fail-soft: returns [] when daemon unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_list("list_memory", limit)
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.memory.list_unavailable",
                extra={"reason": str(exc)},
            )
            return []

    @router.get("/search")
    async def search_memory(
        request: Request,
        q: str = Query(..., min_length=1, description="Search query"),
        limit: int = Query(50, le=500),
    ) -> list[dict]:
        """Semantic search over agent memory.

        Returns [{entry_index, target, content, score}].
        Fail-soft: returns [] when daemon unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_list("search_memory", q, limit)
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.memory.search_unavailable",
                extra={"query": q, "reason": str(exc)},
            )
            return []

    return router
