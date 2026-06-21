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


def _mirror_provider_to_local_repo(request: Request, body) -> None:
    """Best-effort: persist the provider (incl. key) into shell-server's local
    SQLiteProviderRepository so the shell can make its own LLM calls. The daemon
    remains authoritative for the agent; this is a parallel copy for the bridge."""
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        return
    try:
        from hermes.shell_server.providers.domain import (  # noqa: PLC0415
            ProviderAliasConflict,
            ProviderKind,
            new_provider,
        )

        try:
            kind = ProviderKind(body.kind)
        except ValueError:
            logger.warning("provider mirror: unknown kind %s", body.kind)
            return

        prov = new_provider(
            alias=body.alias,
            kind=kind,
            default_model=body.default_model,
            base_url=body.base_url,
            has_api_key=bool(body.api_key),
        )
        try:
            repo.add(provider=prov, api_key=body.api_key)
        except ProviderAliasConflict:
            existing = next((p for p in repo.list_all() if p.alias == body.alias), None)
            if existing is None:
                return
            prov.provider_id = existing.provider_id
            repo.update(provider=prov, api_key=body.api_key)
        if body.set_active:
            repo.set_active(provider_id=prov.provider_id)
    except Exception:  # noqa: BLE001 — never block provider creation
        logger.warning("provider mirror to local repo failed", exc_info=True)


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
    kind: str = Field(min_length=1)
    api_key: str | None = None
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
        draft = {
            "kind": body.kind,
            "set_active": body.set_active,
        }
        if body.api_key:
            draft["api_key"] = body.api_key
        try:
            return await proxy.call_mutator("configure_native_provider", json.dumps(draft))
        except AgentUnavailable as exc:
            _raise_503(exc, "configure_native_provider")

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

        # Mirror into shell-server's local repo so the shell can make its own LLM
        # calls (skill synthesis, the "LiteLLM bridge"). Best-effort: never block
        # provider creation if the mirror fails.
        _mirror_provider_to_local_repo(request, body)
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
        """Delete a configured provider."""
        proxy = request.app.state.dbus_proxy
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
