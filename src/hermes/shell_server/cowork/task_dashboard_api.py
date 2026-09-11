"""Authenticated local task dashboard; failures are never empty success."""

from fastapi import APIRouter, HTTPException, Query, Request

from hermes.tasks.control_plane.domain.ports import AgentUnavailable


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

    return router
