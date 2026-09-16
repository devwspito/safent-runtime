"""Owner-only model selection for an already connected native Codex provider."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from hermes.shell_server.security.owner_confirmation import require_owner_session
from hermes.tasks.control_plane.domain.ports import AgentUnavailable


class SelectNativeModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(pattern=r"^openai-codex$")
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    expected_model: str = Field(max_length=128)


def _checked(result: dict, *, catalog: bool) -> dict:
    code = result.get("code")
    if code:
        safe_codes = {
            "managed_by_enterprise": 403,
            "selection_changed": 409,
            "unsupported_provider": 400,
            "invalid_model": 400,
            "model_not_available": 400,
        }
        status = safe_codes.get(code, 503) if isinstance(code, str) else 503
        raise HTTPException(
            status,
            detail={
                "code": code
                if isinstance(code, str) and code in safe_codes
                else "models_unavailable"
            },
        )
    if result.get("provider_id") != "openai-codex" or not isinstance(
        result.get("active_model"), str
    ):
        raise HTTPException(503, detail={"code": "models_unavailable"})
    clean = {"provider_id": result["provider_id"], "active_model": result["active_model"]}
    if catalog:
        models = result.get("models")
        if (
            not isinstance(models, list)
            or not models
            or any(not isinstance(model, str) for model in models)
        ):
            raise HTTPException(503, detail={"code": "models_unavailable"})
        clean["models"] = models
    return clean


def create_native_models_router() -> APIRouter:
    router = APIRouter(prefix="/native", tags=["providers"])

    @router.get("/models")
    async def models(request: Request) -> dict:
        require_owner_session(request)
        try:
            result = await request.app.state.dbus_proxy.call_dict(
                "list_native_provider_models", "openai-codex"
            )
            return _checked(result, catalog=True)
        except AgentUnavailable:
            raise HTTPException(503, detail={"code": "models_unavailable"}) from None

    @router.patch("/model")
    async def select(request: Request, body: SelectNativeModelRequest) -> dict:
        require_owner_session(request)
        try:
            result = await request.app.state.dbus_proxy.call_mutator(
                "set_native_provider_model",
                body.provider_id,
                body.model,
                body.expected_model,
            )
            return _checked(result, catalog=False)
        except AgentUnavailable:
            raise HTTPException(503, detail={"code": "models_unavailable"}) from None

    return router
