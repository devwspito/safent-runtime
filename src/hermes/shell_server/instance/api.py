"""shell-server instance router — pairing, status, and feature gates.

Endpoints:
  POST /api/v1/instance/pair
    body: {code, cloud_endpoint?}
    Gated by the operator-token middleware (same as all mutating /api/v1/* calls).
    409 if already associated.

  GET  /api/v1/instance/status
    Returns {edition, associated, instance_id?, tenant_id?, paired_at?,
             last_applied_version?, license?}.  Fail-soft (never 5xx).

  GET  /api/v1/instance/features
    Returns {edition, views: [...]}.  Fail-soft.

Feature policy (placeholder until Fase 4 delivers cloud-pushed policies):
  community : ALL views (CE is full-featured).
  associate : chat + coste + tablero (minimum; expanded by Fase 4 policies).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from hermes.instance.association_store import SQLiteAssociationStore

if TYPE_CHECKING:
    from hermes.shell_server.security.secrets import SecretsVault

logger = logging.getLogger("hermes.shell_server.instance.api")

# Canonical sidebar views as defined in frontend/src/components/Layout.tsx::useNavItems.
# Kept here as the authoritative server-side list so feature-gate logic has a single
# source of truth.
_ALL_VIEWS: list[str] = [
    "chat",
    "programadas",
    "agentes",
    "skills",
    "integraciones",
    "mcp",
    "archivos",
    "proveedores",
    "seguridad",
    "memoria",
    "coste",
]

# NOTE: `tablero` is intentionally NOT in _ALL_VIEWS above. It is an always-on
# pure-UI dashboard with no /api/v1 feature surface (useFeatures forces it on),
# so it is not a governable native view. Do NOT "fix" this by adding tablero to
# _ALL_VIEWS — the cloud console mirrors _ALL_VIEWS as its authorable vocabulary
# and treats chat+tablero as always-on (see safent-control-enterprise Agents.tsx
# ALWAYS_ON_VIEWS + tests/test_view_vocabulary_mirror).

# Minimum view set for an associate instance before Fase 4 cloud policies arrive.
_ASSOCIATE_DEFAULT_VIEWS: list[str] = ["chat", "coste", "tablero"]


# ------------------------------------------------------------------
# Pydantic schemas
# ------------------------------------------------------------------


class PairRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)
    cloud_endpoint: str = Field(
        default="https://cloud.safent.run",
        description="Control plane base URL (optional — defaults to the production endpoint).",
        max_length=2048,
    )


class PairResponse(BaseModel):
    edition: str
    instance_id: str
    tenant_id: str
    paired_at: str


class StatusResponse(BaseModel):
    edition: str
    associated: bool
    instance_id: str | None = None
    tenant_id: str | None = None
    paired_at: str | None = None
    last_applied_version: int | None = None
    license: dict | None = None


class FeaturesResponse(BaseModel):
    edition: str
    views: list[str]


# ------------------------------------------------------------------
# Router factory
# ------------------------------------------------------------------


def create_instance_router(db_path: Path, vault: "SecretsVault") -> APIRouter:
    """Create and return the /api/v1/instance router.

    The router is registered in main.py alongside all other routers.
    The operator-token middleware already gates all POST /api/v1/* calls,
    so this router does not re-implement auth checks.
    """
    router = APIRouter(prefix="/api/v1/instance", tags=["instance"])

    def _store() -> SQLiteAssociationStore:
        return SQLiteAssociationStore(db_path=db_path, vault=vault)

    def _pairing_service(cloud_endpoint: str):  # type: ignore[return]
        from hermes.agents_os.application.node_enrollment import NodeEnrollmentService  # noqa: PLC0415
        from hermes.agents_os.application.tenant_binding_service import TenantBindingService  # noqa: PLC0415
        from hermes.instance.infrastructure.http_control_plane_client import HttpControlPlaneClient  # noqa: PLC0415
        from hermes.instance.pairing_service import PairingService  # noqa: PLC0415

        return PairingService(
            enrollment=NodeEnrollmentService(),
            binding=TenantBindingService(),
            store=_store(),
            client=HttpControlPlaneClient(cloud_endpoint=cloud_endpoint),
        )

    @router.post("/pair", response_model=PairResponse)
    async def pair_instance(body: PairRequest) -> PairResponse:
        """Associate this Safent instance with an enterprise tenant.

        409 when already associated (use unpair first).
        """
        from hermes.instance.pairing_service import AlreadyAssociatedError, PairingError  # noqa: PLC0415

        svc = _pairing_service(body.cloud_endpoint)
        try:
            assoc = svc.pair(code=body.code, cloud_endpoint=body.cloud_endpoint)
        except AlreadyAssociatedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PairingError as exc:
            logger.warning("hermes.instance.pair.failed", extra={"reason": str(exc)})
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return PairResponse(
            edition="associate",
            instance_id=assoc.instance_id,
            tenant_id=assoc.tenant_id,
            paired_at=assoc.paired_at,
        )

    @router.get("/status", response_model=StatusResponse)
    async def instance_status() -> StatusResponse:
        """Return current edition and association details.

        Always returns 200 (fail-soft) — degraded state on any storage error.
        """
        try:
            store = _store()
            assoc = store.get()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.instance.status.error", extra={"reason": str(exc)})
            return StatusResponse(edition="community", associated=False)

        if assoc is None:
            return StatusResponse(edition="community", associated=False)

        return StatusResponse(
            edition=store.edition(),
            associated=store.is_associated(),
            instance_id=assoc.instance_id,
            tenant_id=assoc.tenant_id,
            paired_at=assoc.paired_at,
            last_applied_version=assoc.last_applied_version,
            license=assoc.license or None,
        )

    @router.get("/features", response_model=FeaturesResponse)
    async def instance_features() -> FeaturesResponse:
        """Return the edition and the list of enabled views.

        CE: all views (full-featured community edition).
        Associate (no cloud policy yet): minimum placeholder set.
        Fail-soft: returns CE defaults on storage errors.
        """
        try:
            store = _store()
            edition = store.edition()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.instance.features.error", extra={"reason": str(exc)})
            return FeaturesResponse(edition="community", views=list(_ALL_VIEWS))

        if edition == "community":
            return FeaturesResponse(edition=edition, views=list(_ALL_VIEWS))

        # Associate: the granted views travel in license.views (LicenseSpec),
        # persisted by config_sync via store.update_license(). The sidebar MUST
        # reflect the same set the FeatureGuardMiddleware enforces — otherwise the
        # UI hides/shows the wrong views. Fall back to the minimum associate set
        # until the first cloud policy lands.
        views = list(_ASSOCIATE_DEFAULT_VIEWS)
        try:
            assoc = store.get()
            raw_views = assoc.license.get("views") if assoc and assoc.license else None
            if isinstance(raw_views, list) and raw_views:
                views = list(raw_views)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.instance.features.license_read_error", extra={"reason": str(exc)})
        return FeaturesResponse(edition=edition, views=views)

    return router
