"""Image generation (FAL.ai) key REST API — D-Bus surface, mirrors web_search_api.

Endpoints:
  GET    /api/v1/integrations/image-generation       provider + has_key + model
  POST   /api/v1/integrations/image-generation/key    store the FAL.ai API key
  DELETE /api/v1/integrations/image-generation/key    remove the FAL.ai API key

Setting the key takes effect immediately (the daemon injects FAL_KEY into
os.environ live AND persists it to HERMES_HOME/.env). The key is NEVER
echoed back in any response, only the `has_key` flag.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes.shell_server.integrations.image_generation_api")

_PROVIDER = "fal"


class SetImageGenerationKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1, max_length=400)


class ImageGenerationKeyResponse(BaseModel):
    has_key: bool


class ImageGenerationStatusResponse(BaseModel):
    provider: str
    has_key: bool
    model: str | None = None


def create_image_generation_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/integrations/image-generation", tags=["image-generation"]
    )

    @router.get("", response_model=ImageGenerationStatusResponse)
    async def image_generation_status(request: Request) -> ImageGenerationStatusResponse:
        """Whether the FAL.ai API key is configured (fail-soft)."""
        proxy = request.app.state.dbus_proxy
        try:
            status = await proxy.call_dict("get_image_generation_status")
        except AgentUnavailable as exc:
            logger.warning("hermes.image_generation.status_unavailable", extra={"reason": str(exc)})
            return ImageGenerationStatusResponse(provider=_PROVIDER, has_key=False, model=None)
        return ImageGenerationStatusResponse(
            provider=str(status.get("provider") or _PROVIDER),
            has_key=bool(status.get("has_key", False)),
            model=status.get("model"),
        )

    @router.post("/key", response_model=ImageGenerationKeyResponse)
    async def set_image_generation_key(
        body: SetImageGenerationKeyRequest, request: Request
    ) -> ImageGenerationKeyResponse:
        """Store the FAL.ai API key (takes effect immediately, never echoed back)."""
        proxy = request.app.state.dbus_proxy
        try:
            result = await proxy.call_mutator("set_image_generation_api_key", body.api_key)
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.image_generation.mutator_unavailable", extra={"reason": str(exc)}
            )
            raise HTTPException(
                status_code=503,
                detail={"code": "agent_unavailable", "message": "El agente no está disponible."},
            ) from exc
        if not result.get("ok", False):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_api_key", "message": "No se pudo guardar la clave."},
            )
        return ImageGenerationKeyResponse(has_key=True)

    @router.delete("/key", response_model=ImageGenerationKeyResponse)
    async def delete_image_generation_key(request: Request) -> ImageGenerationKeyResponse:
        """Remove the stored FAL.ai API key."""
        proxy = request.app.state.dbus_proxy
        try:
            result = await proxy.call_mutator("delete_image_generation_api_key")
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.image_generation.mutator_unavailable", extra={"reason": str(exc)}
            )
            raise HTTPException(
                status_code=503,
                detail={"code": "agent_unavailable", "message": "El agente no está disponible."},
            ) from exc
        if not result.get("ok", False):
            raise HTTPException(
                status_code=422,
                detail={"code": "delete_failed", "message": "No se pudo eliminar la clave."},
            )
        return ImageGenerationKeyResponse(has_key=False)

    return router
