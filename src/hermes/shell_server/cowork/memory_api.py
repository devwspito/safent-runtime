"""Memory REST API — D-Bus surface for agent memory retrieval and deletion.

Endpoints:
  GET    /api/v1/memory           list recent memory entries (content truncated at 200 chars)
  GET    /api/v1/memory/search    semantic search (?q=query)
  GET    /api/v1/memory/{id}      fetch one entry with FULL content by id '{target}:{index}'
  DELETE /api/v1/memory/{id}      forget one entry by id '{target}:{index}'

The list endpoint returns {id, target, content_truncated, entry_index} — content
is deliberately truncated at the D-Bus boundary as a bulk-PII guard.  The detail
endpoint returns the full content for a single, explicitly requested entry.

DELETE is idempotent (200 even when already removed). 403 on authorization
failure. 503 when daemon is unavailable.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Query, Request

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

    @router.get("/{entry_id}")
    async def get_memory_entry(request: Request, entry_id: str) -> dict:
        """Fetch a single memory entry by its composite id '{target}:{index}'.

        Returns {id, target, content, entry_index} with the FULL content — not
        truncated.  Use this to populate a detail drawer in the UI.

        Returns 404 when the entry does not exist.
        Returns 503 when the daemon is unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            result = await proxy.call_dict("get_memory_entry", entry_id)
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.memory.get_entry_unavailable",
                extra={"entry_id": entry_id, "reason": str(exc)},
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "agent_unavailable",
                    "message": "El agente no está disponible.",
                },
            ) from exc
        if not result:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": "memory entry not found"},
            )
        return result

    @router.put("/{entry_id}")
    async def update_memory_entry(
        request: Request,
        entry_id: str,
        content: str = Body(..., embed=True, min_length=1),
    ) -> dict:
        """Edit the content of one memory entry by its id '{target}:{index}'.

        Body: {"content": "<new text>"}.
        Returns {ok:true, updated:bool} on success.
        400 when the content is empty, the entry is missing, or the new content
        is rejected by the PII/injection guard (fail-closed).
        503 when the daemon is unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            result = await proxy.call_dict("update_memory_entry", entry_id, content)
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.memory.update_unavailable",
                extra={"entry_id": entry_id, "reason": str(exc)},
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "agent_unavailable",
                    "message": "El agente no está disponible.",
                },
            ) from exc
        if not result.get("ok"):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": result.get("code", "update_failed"),
                    "message": result.get("error", "unknown"),
                },
            )
        return result

    @router.delete("/{entry_id}")
    async def forget_memory_entry(request: Request, entry_id: str) -> dict:
        """Forget (delete) one memory entry by its composite id '{target}:{index}'.

        Idempotent: returns {ok: true} even when the entry was already removed.
        403 on authorization failure; 503 when daemon is unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            result = await proxy.call_dict("forget_memory_entry", entry_id)
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.memory.delete_unavailable",
                extra={"entry_id": entry_id, "reason": str(exc)},
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "agent_unavailable",
                    "message": "El agente no está disponible.",
                },
            ) from exc
        if not result.get("ok"):
            raise HTTPException(
                status_code=400,
                detail={"code": "delete_failed", "message": result.get("error", "unknown")},
            )
        return result

    return router
