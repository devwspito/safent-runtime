"""Owner-only policy mutations. Community uses confirmation, never MFA.

The danger-approval switch controls HITL, not authentication. It cannot be
changed by the internal daemon bearer even when it is already disabled.
The network/HTTP authentication boundary still protects reads.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from hermes.capabilities.tool_policy import PolicyUnavailableError, Preset, ToolPolicyStore
from hermes.shell_server.security.owner_confirmation import require_owner_session

logger = logging.getLogger("hermes.shell_server.cowork.policies_api")


def _policy_call[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (PolicyUnavailableError, OSError) as exc:
        # No fabricated preset and no raw file contents/paths in the response.
        raise HTTPException(status_code=503, detail={
            "error": "policy_unavailable",
            "message": "Política no disponible. Las herramientas siguen protegidas.",
        }) from exc


class _PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PresetBody(_PolicyBody):
    preset: Literal["equilibrado", "permisivo", "bloqueado"]


class ToolBody(_PolicyBody):
    tool: str = Field(min_length=1, max_length=200)
    enabled: StrictBool


class ToolsBody(_PolicyBody):
    tools: dict[str, StrictBool] = Field(min_length=1, max_length=2000)


class DangerApprovalBody(_PolicyBody):
    enabled: StrictBool


def create_policies_router(policy: ToolPolicyStore | None = None) -> APIRouter:
    router = APIRouter()
    store = policy or ToolPolicyStore()
    # Structural enforcement for every mutation registered on this subrouter.
    writes = APIRouter(dependencies=[Depends(require_owner_session)])

    @router.get("/api/v1/policies")
    async def get_policies() -> dict:
        return _policy_call(store.snapshot)

    @writes.post("/api/v1/policies/preset")
    async def set_preset(body: PresetBody) -> dict:
        _policy_call(lambda: store.apply_preset(Preset(body.preset)))
        try:
            from hermes.shell_server.egress_api import (  # noqa: PLC0415
                apply_browser_egress_for_preset,
            )

            apply_browser_egress_for_preset()
        except Exception:  # noqa: BLE001
            logger.warning("hermes.cowork.policies.egress_apply_failed", exc_info=True)
        logger.info("hermes.cowork.policies.preset_applied preset=%s", body.preset)
        return {"ok": True, "preset": body.preset}

    @writes.post("/api/v1/policies/tool")
    async def set_tool(body: ToolBody) -> dict:
        _policy_call(lambda: store.set_tool(body.tool, body.enabled))
        logger.info("hermes.cowork.policies.tool_set tool=%s enabled=%s", body.tool, body.enabled)
        return {"ok": True, "tool": body.tool, "enabled": body.enabled}

    @writes.post("/api/v1/policies/tools")
    async def set_tools(body: ToolsBody) -> dict:
        _policy_call(lambda: store.set_tools(body.tools))
        logger.info("hermes.cowork.policies.tools_set count=%d", len(body.tools))
        return {"ok": True, "count": len(body.tools)}

    @writes.post("/api/v1/policies/approval_on_dangers")
    async def set_approval_on_dangers(body: DangerApprovalBody) -> dict:
        _policy_call(lambda: store.set_approval_on_dangers(body.enabled))
        logger.info("hermes.cowork.policies.approval_on_dangers_set enabled=%s", body.enabled)
        return {"ok": True, "approval_on_dangers": body.enabled}

    router.include_router(writes)
    return router
