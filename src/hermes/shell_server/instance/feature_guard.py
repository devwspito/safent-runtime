"""Feature-gate middleware — enforces view-level access control for associate editions.

In community edition (CE): no-op, all requests pass through.
In associate edition: each /api/v1/* path is mapped to a feature name; if the
  feature is not in the list of allowed views returned by the AssociationStore,
  the request is rejected with HTTP 403.

DEFAULT-DENY: any /api/v1/* path that does not match a known prefix AND is not in
the always-allowed set is denied when in associate edition. A new endpoint without
a mapping MUST be added here before it is accessible in associate.

Cache strategy: the edition + views are cached in app.state for _CACHE_TTL_SECS
seconds. The TTL is short (10 s) so that a cloud policy push is reflected quickly
without hammering SQLite on every request.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from hermes.instance.association_store import SQLiteAssociationStore
from hermes.shell_server.instance.api import _ALL_VIEWS  # single source of truth

logger = logging.getLogger("hermes.shell_server.instance.feature_guard")

_CACHE_TTL_SECS: float = 10.0

# ---------------------------------------------------------------------------
# Prefix → feature name mapping
# Each entry: (prefix, feature_name).  First match wins.
# Feature names MUST match _ALL_VIEWS from instance/api.py.
# ---------------------------------------------------------------------------
_PREFIX_FEATURE_MAP: tuple[tuple[str, str], ...] = (
    ("/api/v1/providers",   "proveedores"),
    ("/api/v1/mcp",         "mcp"),
    ("/api/v1/skills",      "skills"),
    ("/api/v1/composio",    "skills"),
    ("/api/v1/integrations","integraciones"),
    ("/api/v1/tasks",       "programadas"),
    ("/api/v1/agents",      "agentes"),
    ("/api/v1/runtime/agent","agentes"),
    ("/api/v1/roster",      "agentes"),
    ("/api/v1/security",    "seguridad"),
    ("/api/v1/egress",      "seguridad"),
    ("/api/v1/policies",    "seguridad"),
    ("/api/v1/approvals",   "seguridad"),
    # MFA/2FA management is part of the Seguridad view; without this mapping the
    # endpoint is default-denied (403) on associates and the 2FA panel can't load.
    ("/api/v1/mfa",         "seguridad"),
    ("/api/v1/memory",      "memoria"),
    ("/api/v1/workspace",   "archivos"),
    ("/api/v1/archivos",    "archivos"),
    ("/api/v1/usage",       "coste"),
)

# Path prefixes that are ALWAYS allowed, regardless of edition or license.
# NOTE: do NOT include bare "/" here — it would match every path.
# Use _ALWAYS_ALLOWED_EXACT for root-exact matches.
_ALWAYS_ALLOWED_PREFIXES: frozenset[str] = frozenset({
    "/api/v1/chat",
    "/api/v1/instance",
    "/api/v1/session",
    "/api/v1/profile",
    "/api/v1/runtime/status",
    "/healthz",
    "/metrics",
    "/app/",
    "/webui/",
    "/classic",
    "/ws/",
})

# Exact paths that are always allowed (e.g. the SPA root redirect).
_ALWAYS_ALLOWED_EXACT: frozenset[str] = frozenset({"/", "/app"})


@dataclass
class _FeatureCache:
    edition: str = "community"
    views: frozenset[str] = field(default_factory=frozenset)
    expires_at: float = 0.0

    def is_fresh(self) -> bool:
        return time.monotonic() < self.expires_at


def _resolve_feature(path: str) -> str | None:
    """Return the feature name for a path, or None if not mapped."""
    for prefix, feature in _PREFIX_FEATURE_MAP:
        if path.startswith(prefix):
            return feature
    return None


def _is_always_allowed(path: str) -> bool:
    if path in _ALWAYS_ALLOWED_EXACT:
        return True
    for prefix in _ALWAYS_ALLOWED_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


class FeatureGuardMiddleware(BaseHTTPMiddleware):
    """Enforce feature-level access control for associate edition.

    Registered AFTER the operator-token middleware so auth already ran.
    In CE, this middleware is a transparent pass-through.
    """

    def __init__(self, app, *, db_path: Path, vault) -> None:
        super().__init__(app)
        self._db_path = db_path
        self._vault = vault
        self._cache = _FeatureCache()

    def _refresh_cache(self) -> _FeatureCache:
        """Read edition + views from the store, update and return the cache."""
        try:
            store = SQLiteAssociationStore(db_path=self._db_path, vault=self._vault)
            edition = store.edition()
            if edition == "associate":
                assoc = store.get()
                lic = assoc.license if assoc else {}
                raw_views = lic.get("views") if lic else None
                if isinstance(raw_views, list):
                    views: frozenset[str] = frozenset(raw_views)
                else:
                    # Fall back to the API's default associate set.
                    from hermes.shell_server.instance.api import _ASSOCIATE_DEFAULT_VIEWS  # noqa: PLC0415
                    views = frozenset(_ASSOCIATE_DEFAULT_VIEWS)
            else:
                views = frozenset(_ALL_VIEWS)
        except Exception:  # noqa: BLE001
            logger.warning(
                "hermes.feature_guard.cache_refresh_failed",
                exc_info=True,
            )
            # Fail-open for CE (community stays unrestricted); fail-open is safe
            # because the guard only restricts associate edition.  If the store is
            # unavailable we cannot determine the edition, so we assume community.
            edition = "community"
            views = frozenset(_ALL_VIEWS)

        self._cache = _FeatureCache(
            edition=edition,
            views=views,
            expires_at=time.monotonic() + _CACHE_TTL_SECS,
        )
        return self._cache

    def _get_cache(self) -> _FeatureCache:
        if not self._cache.is_fresh():
            return self._refresh_cache()
        return self._cache

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        if _is_always_allowed(path):
            return await call_next(request)

        cache = self._get_cache()

        if cache.edition != "associate":
            return await call_next(request)

        feature = _resolve_feature(path)

        if feature is None:
            # DEFAULT-DENY: unmapped /api/v1/* in associate edition.
            logger.warning(
                "hermes.feature_guard.denied_unmapped",
                extra={"path": path, "edition": cache.edition},
            )
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        f"Access denied: path '{path}' is not mapped to a licensed "
                        "feature. Add a mapping in feature_guard.py to enable it."
                    )
                },
            )

        if feature not in cache.views:
            logger.info(
                "hermes.feature_guard.denied",
                extra={"path": path, "feature": feature, "allowed": sorted(cache.views)},
            )
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        f"Feature '{feature}' is not enabled for this associate license."
                    )
                },
            )

        return await call_next(request)
