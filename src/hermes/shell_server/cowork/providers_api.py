"""Providers REST API — full D-Bus surface for LLM provider management.

Endpoints:
  GET    /api/v1/providers              configured providers (read-only, no secrets)
  GET    /api/v1/providers/native       native Hermes provider catalog (42+ entries)
  POST   /api/v1/providers              add a custom provider
  POST   /api/v1/providers/native       configure a native provider
  POST   /api/v1/providers/{id}/activate   set_active_provider
  POST   /api/v1/providers/{id}/test       test_provider (reachability + auth)
  DELETE /api/v1/providers/{id}            delete_provider
  POST   /api/v1/providers/{id}/oauth/start  start_provider_oauth
  GET    /api/v1/providers/oauth/{session_id}  get_provider_oauth_status

Security:
  - No API keys returned in any GET response.
  - Mutators receive a signed OperatorToken via DbusRuntimeProxy.call_mutator().
  - fail-soft for GET lists; fail-hard 503 for mutators (CTRL-P1-11).
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes.shell_server.cowork.providers_api")


async def _reject_if_cloud_managed(
    proxy, *, provider_id: str | None = None, alias: str | None = None
) -> None:
    """Gating: a cloud-managed provider is owned by the org's policy — the local
    operator may not edit/delete/overwrite it ("el empleado no lo toca").

    Enforced HERE because this REST layer is the operator's only entry point; the
    config-sync applier mutates managed_by="cloud" rows via D-Bus directly (never
    through REST), so it is unaffected and stays the sole owner of these rows.
    Fail-open on a daemon lookup error: the mutator call right after will surface
    the 503 itself, and the next sync reconciles regardless.
    """
    try:
        providers = await proxy.call_list("list_providers")
    except AgentUnavailable:
        return
    for p in providers:
        if p.get("managed_by") != "cloud":
            continue
        if provider_id is not None and p.get("provider_id") == provider_id:
            raise HTTPException(
                status_code=403,
                detail="Este proveedor lo gestiona la política de tu organización "
                "y no puede modificarse desde aquí.",
            )
        if alias is not None and p.get("alias") == alias:
            raise HTTPException(
                status_code=403,
                detail="Ya existe un proveedor con ese alias gestionado por tu "
                "organización.",
            )


# ------------------------------------------------------------------
# Pydantic request schemas
# ------------------------------------------------------------------


class AddProviderRequest(BaseModel):
    kind: str = Field(min_length=1, description="Provider kind: openai, anthropic, vllm, etc.")
    alias: str = Field(min_length=1, max_length=120)
    default_model: str = Field(min_length=1)
    base_url: str | None = None
    api_key: str | None = None
    set_active: bool = False


class ConfigureNativeProviderRequest(BaseModel):
    provider_id: str = Field(min_length=1, description="Native registry id, e.g. openai-api, gemini")
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    set_active: bool = False


# ------------------------------------------------------------------
# Router factory
# ------------------------------------------------------------------


def create_providers_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/providers", tags=["providers"])

    @router.get("/native")
    async def list_native_providers(request: Request) -> list[dict]:
        """List the full native Hermes provider catalog.

        Returns name/id/kind/auth-type/configured-state for each provider.
        Fail-soft: returns [] when daemon unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_list("list_native_providers")
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.providers.native.list_unavailable",
                extra={"reason": str(exc)},
            )
            return []

    @router.post("/native", status_code=201)
    async def configure_native_provider(
        request: Request, body: ConfigureNativeProviderRequest
    ) -> dict:
        """Configure a native Hermes provider (e.g. Anthropic, Claude Max, etc.)."""
        import json  # noqa: PLC0415

        proxy = request.app.state.dbus_proxy
        # The daemon verb (ConfigureNativeProvider) reads provider_id/api_key/model/
        # base_url. Sending `kind` here left provider_id empty → "provider desconocido".
        draft = {
            "provider_id": body.provider_id,
            "set_active": body.set_active,
        }
        if body.api_key:
            draft["api_key"] = body.api_key
        if body.model:
            draft["model"] = body.model
        if body.base_url:
            draft["base_url"] = body.base_url
        try:
            return await proxy.call_mutator("configure_native_provider", json.dumps(draft))
        except AgentUnavailable as exc:
            _raise_503(exc, "configure_native_provider")

    @router.get("/native/active")
    async def get_native_active(request: Request) -> dict:
        """The native provider currently set as the model (config.yaml), {} if none.

        Native-configured providers live in a separate store from the shell-server
        repo; the UI merges this into its configured list so a native catalogue
        provider the user just added is actually visible + marked active.
        """
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_dict("get_native_active")
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.providers.native_active_unavailable", extra={"reason": str(exc)}
            )
            return {}

    @router.get("/oauth/{session_id}")
    async def get_provider_oauth_status(request: Request, session_id: str) -> dict:
        """Poll the status of a provider OAuth session."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_dict("get_provider_oauth_status", session_id)
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.providers.oauth.status_unavailable",
                extra={"session_id": session_id, "reason": str(exc)},
            )
            return {"status": "unknown"}

    @router.post("", status_code=201)
    async def add_provider(request: Request, body: AddProviderRequest) -> dict:
        """Add a new custom LLM provider."""
        import json  # noqa: PLC0415

        proxy = request.app.state.dbus_proxy
        await _reject_if_cloud_managed(proxy, alias=body.alias)
        draft = {
            "kind": body.kind,
            "alias": body.alias,
            "default_model": body.default_model,
            "set_active": body.set_active,
        }
        if body.base_url:
            draft["base_url"] = body.base_url
        if body.api_key:
            draft["api_key"] = body.api_key
        try:
            result = await proxy.call_mutator("add_provider", json.dumps(draft))
        except AgentUnavailable as exc:
            _raise_503(exc, "add_provider")
            return {}  # unreachable; _raise_503 raises

        return result

    @router.post("/{provider_id}/activate")
    async def activate_provider(request: Request, provider_id: str) -> dict:
        """Set a provider as the active model source."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_mutator("set_active_provider", provider_id)
        except AgentUnavailable as exc:
            _raise_503(exc, "set_active_provider")

    @router.post("/{provider_id}/test")
    async def test_provider(request: Request, provider_id: str) -> dict:
        """Test reachability and authentication of a provider."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_dict("test_provider", provider_id)
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.providers.test_unavailable",
                extra={"provider_id": provider_id, "reason": str(exc)},
            )
            return {"ok": False, "error": "daemon_unavailable"}

    @router.delete("/{provider_id}", status_code=204)
    async def delete_provider(request: Request, provider_id: str) -> None:
        """Delete a configured provider (rejected for cloud-managed rows)."""
        proxy = request.app.state.dbus_proxy
        await _reject_if_cloud_managed(proxy, provider_id=provider_id)
        try:
            await proxy.call_bool("delete_provider", provider_id)
        except AgentUnavailable as exc:
            _raise_503(exc, "delete_provider")

    @router.post("/{provider_id}/oauth/start")
    async def start_provider_oauth(request: Request, provider_id: str) -> dict:
        """Initiate OAuth for a provider. Returns {session_id, redirect_url}."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_mutator("start_provider_oauth", provider_id)
        except AgentUnavailable as exc:
            _raise_503(exc, "start_provider_oauth")

    return router


def _raise_503(exc: AgentUnavailable, operation: str) -> None:
    logger.warning(
        "hermes.providers.mutator_unavailable",
        extra={"operation": operation, "reason": str(exc)},
    )
    raise HTTPException(
        status_code=503,
        detail={
            "code": "agent_unavailable",
            "message": "El agente no está disponible. Comprueba que hermes-runtime está activo.",
        },
    ) from exc
