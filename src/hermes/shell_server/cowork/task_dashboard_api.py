"""Authenticated local task dashboard; failures are never empty success."""

import asyncio
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from hermes.shell_server.security.owner_confirmation import require_owner_session
from hermes.tasks.control_plane.domain.ports import AgentUnavailable, UnknownTask
from hermes.tasks.domain.ports import TaskStatus


def create_task_dashboard_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

    @router.get("/dashboard")
    async def dashboard(request: Request, limit: int = Query(100, ge=1, le=200)) -> dict:
        try:
            result = await request.app.state.dbus_proxy.call_operator_dict(
                "get_tasks_dashboard",
                limit,
            )
            if (
                result.get("available") is not True
                or not isinstance(result.get("tasks"), list)
                or not isinstance(result.get("has_more"), bool)
            ):
                raise AgentUnavailable("invalid task dashboard response")
            return result
        except AgentUnavailable as exc:
            raise HTTPException(
                status_code=503, detail={"code": "task_dashboard_unavailable"}
            ) from exc

    @router.get("/{task_id}/status")
    async def task_status(request: Request, task_id: UUID) -> dict:
        """Exact durable state for stream recovery, never recent-list inference.

        Owner-only and read-only. Do not expose provider error text, instruction,
        enqueued_by, or a stream path supplied by the daemon.
        """
        require_owner_session(request)
        try:
            result = await asyncio.wait_for(
                request.app.state.control_plane.get_task_status(task_id=task_id), timeout=5,
            )
            if (
                result.task_id != task_id
                or result.status not in {status.value for status in TaskStatus}
                or type(result.attempts) is not int
                or result.attempts < 0
            ):
                raise ValueError("invalid task status response")
            return {"task_id": str(task_id), "status": result.status, "attempts": result.attempts}
        except UnknownTask:
            raise HTTPException(404, detail={"code": "task_not_found"}) from None
        except Exception:  # noqa: BLE001 — unavailable/malformed is never terminal evidence
            raise HTTPException(503, detail={"code": "task_status_unavailable"}) from None

    return router
