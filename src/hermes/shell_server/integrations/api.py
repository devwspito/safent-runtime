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

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from hermes.integrations.composio.composio_client import (
    ComposioApiError,
    ComposioClient,
)
from hermes.shell_server.integrations.domain import IntegrationNotFound
from hermes.shell_server.integrations.repo import SQLiteIntegrationsRepository
from hermes.shell_server.security.secrets import SecretsVault

logger = logging.getLogger(__name__)

_KIND = "composio"


# ----------------------------------------------------------------
# Pydantic schemas
# ----------------------------------------------------------------


class SetApiKeyRequest(BaseModel):
    api_key: str = Field(min_length=1)
    entity_id: str = Field(default="default", min_length=1)


class ComposioStatusResponse(BaseModel):
    has_key: bool
    enabled: bool
    entity_id: str


class ToolkitItem(BaseModel):
    slug: str
    name: str
    description: str


class ConnectedAccountItem(BaseModel):
    id: str
    toolkit_slug: str
    entity_id: str
    status: str


class ConnectRequest(BaseModel):
    toolkit_slug: str = Field(min_length=1)
    entity_id: str | None = None
    redirect_url: str | None = None


class ConnectResponse(BaseModel):
    connected_account_id: str
    redirect_url: str
    status: str


# ----------------------------------------------------------------
# Router factory
# ----------------------------------------------------------------


def create_integrations_router(db_path: Path) -> APIRouter:
    """Create the integrations API router.

    Follows the same factory pattern as create_training_router so that
    the db_path is bound at construction time and tests can inject a
    temp path without patching globals.
    """
    _init_schema(db_path)
    router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])

    def _repo() -> SQLiteIntegrationsRepository:
        return SQLiteIntegrationsRepository(db_path=db_path, vault=SecretsVault())

    # ----------------------------------------------------------------
    # Store / rotate API key
    # ----------------------------------------------------------------

    @router.post("/composio/key", response_model=ComposioStatusResponse)
    async def set_composio_key(body: SetApiKeyRequest) -> ComposioStatusResponse:
        """Store the Composio API key (encrypted).

        The key is NEVER echoed back.  Only the `has_key` flag is returned.
        """
        integration = _repo().set_credential(
            kind=_KIND,
            api_key=body.api_key,
            entity_id=body.entity_id,
        )
        logger.info("hermes.integrations.composio.key_stored")
        return ComposioStatusResponse(
            has_key=integration.has_api_key,
            enabled=integration.enabled,
            entity_id=integration.entity_id,
        )

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
        client = _build_client(_repo())
        try:
            toolkits = await client.list_toolkits(search=search, limit=limit)
        except ComposioApiError as exc:
            raise HTTPException(502, f"Composio error: {exc}") from exc
        return [
            ToolkitItem(slug=t.slug, name=t.name, description=t.description)
            for t in toolkits
        ]

    # ----------------------------------------------------------------
    # Connected accounts
    # ----------------------------------------------------------------

    @router.get("/composio/connected", response_model=list[ConnectedAccountItem])
    async def list_connected() -> list[ConnectedAccountItem]:
        """List accounts connected via Composio for the configured entity_id."""
        repo = _repo()
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
            )
            for a in accounts
        ]

    # ----------------------------------------------------------------
    # Initiate OAuth connection
    # ----------------------------------------------------------------

    @router.post("/composio/connect", response_model=ConnectResponse)
    async def connect_app(body: ConnectRequest) -> ConnectResponse:
        """Initiate OAuth for a toolkit; returns the redirect URL for the user."""
        repo = _repo()
        client = _build_client(repo)
        entity_id = body.entity_id or _get_entity_id(repo)
        try:
            result = await client.initiate_connection(
                toolkit_slug=body.toolkit_slug,
                entity_id=entity_id,
                redirect_url=body.redirect_url,
            )
        except ComposioApiError as exc:
            raise HTTPException(502, f"Composio error: {exc}") from exc
        return ConnectResponse(
            connected_account_id=result.connected_account_id,
            redirect_url=result.redirect_url,
            status=result.status,
        )

    # ----------------------------------------------------------------
    # Delete connection
    # ----------------------------------------------------------------

    @router.delete("/composio/connected/{connection_id}")
    async def delete_connection(connection_id: str) -> dict:
        """Delete a connected account from Composio cloud."""
        client = _build_client(_repo())
        try:
            await client.delete_connection(connection_id)
        except ComposioApiError as exc:
            raise HTTPException(502, f"Composio error: {exc}") from exc
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
    return ComposioClient(api_key=api_key)


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
