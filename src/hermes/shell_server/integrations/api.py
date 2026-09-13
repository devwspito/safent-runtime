"""Integrations REST router: Composio API key + connected accounts.

Mounted at /api/v1/integrations/* in the shell-server.

Security rules:
  - The Composio API key is NEVER returned in any response (only `has_key` flag).
  - OAuth tokens for user apps live exclusively in Composio cloud; we never
    store them.
  - All credential storage goes through SecretsVault (AES-GCM-256).

Endpoints:
  POST   /api/v1/integrations/composio/key              store / rotate API key
  GET    /api/v1/integrations/composio/status            has_key + enabled flag
  GET    /api/v1/integrations/composio/toolkits          catalog (proxied)
  GET    /api/v1/integrations/composio/connected         list connected accounts
  POST   /api/v1/integrations/composio/connect           initiate OAuth
  DELETE /api/v1/integrations/composio/connected/{id}   delete connection
"""

from __future__ import annotations

import asyncio
import logging
from http import HTTPStatus
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from hermes.integrations.composio.composio_client import (
    ComposioApiError,
    ComposioClient,
)
from hermes.shell_server.integrations.domain import IntegrationNotFound
from hermes.shell_server.integrations.repo import SQLiteIntegrationsRepository
from hermes.shell_server.security.owner_confirmation import require_owner_session
from hermes.shell_server.security.secrets import SecretsVault

logger = logging.getLogger(__name__)

_KIND = "composio"


# ----------------------------------------------------------------
# Pydantic schemas
# ----------------------------------------------------------------


class SetApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1)


class ComposioStatusResponse(BaseModel):
    has_key: bool
    enabled: bool
    entity_id: str


class ToolkitItem(BaseModel):
    slug: str
    name: str
    description: str
    oauth_simple: bool = False
    managed_auth_available: bool | None = None
    setup_required: bool = False


class AuthConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auth_config_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")


class AuthConfigResponse(BaseModel):
    toolkit_slug: str
    auth_config_id: str | None


AdsToolkit = Literal["googleads", "metaads"]


class ConnectedAccountItem(BaseModel):
    id: str
    toolkit_slug: str
    entity_id: str
    status: str
    auth_config_id: str = ""


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    toolkit_slug: str = Field(min_length=1)
    redirect_url: str | None = None


class ConnectResponse(BaseModel):
    connected_account_id: str
    redirect_url: str
    status: str


# ----------------------------------------------------------------
# Router factory
# ----------------------------------------------------------------


def create_integrations_router(db_path: Path) -> APIRouter:  # noqa: PLR0915 - explicit scoped endpoints
    """Create the integrations API router.

    db_path is bound at construction time so tests can inject a temp path
    without patching globals.
    """
    _init_schema(db_path)
    router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])

    def _repo() -> SQLiteIntegrationsRepository:
        return SQLiteIntegrationsRepository(db_path=db_path, vault=SecretsVault())

    async def _refresh_ads(request: Request) -> None:
        """Push changes promptly; background renewal remains bounded/fail-closed."""
        refresh = getattr(request.app.state, "composio_lease_refresh", None)
        if refresh is None:
            return
        try:
            await asyncio.wait_for(refresh.ensure(force=True), timeout=12)
        except (TimeoutError, RuntimeError):
            logger.warning("hermes.integrations.ads_refresh_pending")

    # ----------------------------------------------------------------
    # Store / rotate API key
    # ----------------------------------------------------------------

    @router.post("/composio/key", response_model=ComposioStatusResponse)
    async def set_composio_key(body: SetApiKeyRequest, request: Request) -> ComposioStatusResponse:
        """Store the Composio API key (encrypted).

        The key is NEVER echoed back.  Only the `has_key` flag is returned.
        """
        require_owner_session(request)
        repo = _repo()
        previous = repo.get_or_none(kind=_KIND)
        integration = repo.set_credential(
            kind=_KIND,
            api_key=body.api_key,
            # Existing installs retain their connections. New installs cannot
            # collide through the historical shared entity "default".
            entity_id=previous.entity_id if previous else f"safent-{uuid4()}",
        )
        logger.info("hermes.integrations.composio.key_stored")
        await _refresh_ads(request)
        return ComposioStatusResponse(
            has_key=integration.has_api_key,
            enabled=integration.enabled,
            entity_id=integration.entity_id,
        )

    @router.get("/composio/auth-configs/{toolkit_slug}", response_model=AuthConfigResponse)
    async def get_auth_config(toolkit_slug: AdsToolkit, request: Request) -> AuthConfigResponse:
        require_owner_session(request)
        return AuthConfigResponse(
            toolkit_slug=toolkit_slug,
            auth_config_id=_repo().auth_config_ids().get(toolkit_slug),
        )

    @router.post("/composio/ads/prepare", response_model=dict[str, bool])
    async def prepare_ads(request: Request) -> dict[str, bool]:
        """Owner can prepare Ads using the stored key, never by copying it again."""
        require_owner_session(request)
        from hermes.shell_server.integrations.ads_setup import (  # noqa: PLC0415
            prepare_composio_ads_configs,
        )

        readiness = await prepare_composio_ads_configs(db_path)
        await _refresh_ads(request)
        return readiness

    @router.put("/composio/auth-configs/{toolkit_slug}", response_model=AuthConfigResponse)
    async def select_auth_config(
        toolkit_slug: AdsToolkit, body: AuthConfigRequest, request: Request,
    ) -> AuthConfigResponse:
        require_owner_session(request)
        repo = _repo()
        client = _build_client(repo)
        try:
            await client.validate_auth_config(toolkit_slug, body.auth_config_id)
        except ComposioApiError as exc:
            # Provider errors can contain credential-bearing SDK objects.
            raise HTTPException(
                409, "No se puede usar esta configuración OAuth. "
                "Comprueba que pertenece a esta plataforma y que está activa.",
            ) from exc
        repo.set_auth_config(toolkit_slug=toolkit_slug, auth_config_id=body.auth_config_id)
        await _refresh_ads(request)
        return AuthConfigResponse(toolkit_slug=toolkit_slug, auth_config_id=body.auth_config_id)

    @router.delete("/composio/auth-configs/{toolkit_slug}", response_model=AuthConfigResponse)
    async def clear_auth_config(toolkit_slug: AdsToolkit, request: Request) -> AuthConfigResponse:
        require_owner_session(request)
        _repo().clear_auth_config(toolkit_slug=toolkit_slug)
        await _refresh_ads(request)
        return AuthConfigResponse(toolkit_slug=toolkit_slug, auth_config_id=None)

    # ----------------------------------------------------------------
    # Status
    # ----------------------------------------------------------------

    @router.get("/composio/status", response_model=ComposioStatusResponse)
    async def get_composio_status() -> ComposioStatusResponse:
        """Return whether a Composio API key is configured."""
        integration = _repo().get_or_none(kind=_KIND)
        if integration is None:
            return ComposioStatusResponse(
                has_key=False, enabled=False, entity_id="default"
            )
        return ComposioStatusResponse(
            has_key=integration.has_api_key,
            enabled=integration.enabled,
            entity_id=integration.entity_id,
        )

    # ----------------------------------------------------------------
    # Toolkit catalog
    # ----------------------------------------------------------------

    @router.get("/composio/toolkits", response_model=list[ToolkitItem])
    async def list_toolkits(
        search: str | None = Query(None, description="Filter by name"),
        limit: int = Query(50, le=200),
    ) -> list[ToolkitItem]:
        """List available Composio toolkits (apps the user can connect)."""
        repo = _repo()
        integration = repo.get_or_none(kind=_KIND)
        if integration is None or not integration.has_api_key:
            return []  # fail-soft: no Composio key configured → empty catalog, NOT 503
        client = _build_client(repo)
        try:
            toolkits = await client.list_toolkits(search=search, limit=limit)
        except ComposioApiError as exc:
            raise HTTPException(502, f"Composio error: {exc}") from exc
        return [
            ToolkitItem(
                slug=t.slug, name=t.name, description=t.description,
                oauth_simple=t.oauth_simple, managed_auth_available=t.managed_auth_available,
                setup_required=t.setup_required,
            )
            for t in toolkits
        ]

    # ----------------------------------------------------------------
    # Connected accounts
    # ----------------------------------------------------------------

    @router.get("/composio/connected", response_model=list[ConnectedAccountItem])
    async def list_connected() -> list[ConnectedAccountItem]:
        """List accounts connected via Composio for the configured entity_id."""
        repo = _repo()
        integration = repo.get_or_none(kind=_KIND)
        if integration is None or not integration.has_api_key:
            return []  # fail-soft: no Composio key configured → empty, NOT 503
        client = _build_client(repo)
        entity_id = _get_entity_id(repo)
        try:
            accounts = await client.list_connected_accounts(entity_id)
        except ComposioApiError as exc:
            raise HTTPException(502, f"Composio error: {exc}") from exc
        return [
            ConnectedAccountItem(
                id=a.id,
                toolkit_slug=a.toolkit_slug,
                entity_id=a.entity_id,
                status=a.status,
                auth_config_id=a.auth_config_id,
            )
            for a in accounts
        ]

    # ----------------------------------------------------------------
    # Initiate OAuth connection
    # ----------------------------------------------------------------

    @router.post("/composio/connect", response_model=ConnectResponse)
    async def connect_app(body: ConnectRequest, request: Request) -> ConnectResponse:
        """Initiate OAuth for a toolkit; returns the redirect URL for the user."""
        require_owner_session(request)
        repo = _repo()
        client = _build_client(repo)
        entity_id = _get_entity_id(repo)
        try:
            result = await client.initiate_connection(
                toolkit_slug=body.toolkit_slug,
                entity_id=entity_id,
                redirect_url=body.redirect_url,
            )
        except ComposioApiError as exc:
            raise HTTPException(
                HTTPStatus.CONFLICT if exc.status_code == HTTPStatus.CONFLICT else 502,
                "No se pudo iniciar la conexión. Si falta preparar la aplicación, "
                "debe hacerlo la administración de Safent, no cada usuario.",
            ) from exc
        return ConnectResponse(
            connected_account_id=result.connected_account_id,
            redirect_url=result.redirect_url,
            status=result.status,
        )

    @router.get("/composio/connected/{connection_id}", response_model=ConnectedAccountItem)
    async def get_connection(connection_id: str) -> ConnectedAccountItem:
        repo = _repo()
        try:
            account = await _build_client(repo).get_connected_account(
                connection_id, entity_id=_get_entity_id(repo),
            )
        except ComposioApiError as exc:
            raise HTTPException(
                404 if exc.status_code in {403, 404} else 502,
                "No se puede comprobar esta conexión en tu espacio.",
            ) from exc
        return ConnectedAccountItem(
            id=account.id, toolkit_slug=account.toolkit_slug, entity_id=account.entity_id,
            status=account.status, auth_config_id=account.auth_config_id,
        )

    # ----------------------------------------------------------------
    # Delete connection
    # ----------------------------------------------------------------

    @router.delete("/composio/connected/{connection_id}")
    async def delete_connection(connection_id: str, request: Request) -> dict:
        """Delete a connected account from Composio cloud."""
        require_owner_session(request)
        repo = _repo()
        client = _build_client(repo)
        try:
            await client.delete_connection(connection_id, entity_id=_get_entity_id(repo))
        except ComposioApiError as exc:
            raise HTTPException(
                404 if exc.status_code in {403, 404} else 502,
                "No se puede desconectar esta cuenta de tu espacio.",
            ) from exc
        logger.info(
            "hermes.integrations.composio.connection_deleted",
            extra={"connection_id": connection_id},
        )
        return {"status": "deleted", "connection_id": connection_id}

    return router


# ----------------------------------------------------------------
# Private helpers
# ----------------------------------------------------------------


def _build_client(repo: SQLiteIntegrationsRepository) -> ComposioClient:
    """Resolve the API key from the vault and build a ComposioClient.

    Raises HTTP 503 if no key is configured.
    """
    try:
        api_key = repo.reveal_api_key(kind=_KIND)
    except IntegrationNotFound:
        api_key = None

    if not api_key:
        raise HTTPException(
            503,
            "Composio API key not configured. "
            "POST /api/v1/integrations/composio/key first.",
        )
    return ComposioClient(api_key=api_key, auth_config_ids=repo.auth_config_ids())


def _get_entity_id(repo: SQLiteIntegrationsRepository) -> str:
    integration = repo.get_or_none(kind=_KIND)
    return integration.entity_id if integration else "default"


def _init_schema(db_path: Path) -> None:
    """Ensure the integrations table exists (idempotent)."""
    import sqlite3  # noqa: PLC0415

    from hermes.shell_server.integrations.repo import _SCHEMA  # noqa: PLC0415

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.executescript("PRAGMA journal_mode=WAL;")
        conn.executescript(_SCHEMA)
    finally:
        conn.close()
