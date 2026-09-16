"""OAuth classification for a toolkit — a product rule, not plumbing.

Shared by Enterprise and Community (spec 002 research.md, Decision 1): if this
diverges, the same organisation key shows a different catalog in each product.

Kept as the exact algorithm already shipped with the Ads companion feature
(v0.9.36) — `_extract_auth_schemes` is a defensive/reflection read (the SDK's
auth-scheme shape has drifted across versions), `_managed_oauth_available`
reads the SDK's own `composio_managed_auth_schemes` declaration directly.
`ADS_TOOLKITS` never count as OAuth-simple until an owner has actually
selected or prepared an auth config for them (see `client.py`).
"""

from __future__ import annotations

from typing import Any

_OAUTH_SIMPLE_SCHEMES = frozenset({"OAUTH2", "OAUTH1"})
ADS_TOOLKITS = frozenset({"googleads", "metaads"})


def _managed_oauth_available(item: Any) -> bool | None:
    """SDK 0.13.1 explicitly distinguishes supported auth from managed auth."""
    schemes = getattr(item, "composio_managed_auth_schemes", None)
    if not isinstance(schemes, (list, tuple)):
        return None
    return bool({str(value).upper() for value in schemes} & _OAUTH_SIMPLE_SCHEMES)


def _extract_auth_schemes(item: Any) -> tuple[str, ...]:
    """Best-effort: lee los auth schemes de un toolkit del SDK (defensivo).

    El SDK de Composio expone el esquema de auth con nombres variables según
    versión. Probamos varias rutas; si no se encuentra, devolvemos () (=desconocido,
    fail-open: no bloqueamos lo que no podemos clasificar).
    """
    candidates: list[Any] = []
    for attr in ("auth_schemes", "authScheme", "auth_scheme"):
        val = getattr(item, attr, None)
        if val:
            candidates = list(val) if isinstance(val, (list, tuple)) else [val]
            break
    if not candidates:
        meta = getattr(item, "meta", None)
        for attr in ("auth_schemes", "auth_config_details", "categories"):
            val = getattr(meta, attr, None) if meta is not None else None
            if val and attr == "auth_schemes":
                candidates = list(val) if isinstance(val, (list, tuple)) else [val]
                break
    out: list[str] = []
    for c in candidates:
        mode = (
            getattr(c, "mode", None) or getattr(c, "auth_mode", None)
            or getattr(c, "scheme", None) or c
        )
        if isinstance(mode, str):
            out.append(mode.strip().upper())
    return tuple(out)


def _is_oauth_simple(schemes: tuple[str, ...]) -> bool:
    """OAuth simple si HAY algún esquema OAuth, o si no se pudo determinar (fail-open)."""
    if not schemes:
        return True  # desconocido → no bloquear (la conexión gestionada decidirá)
    return bool(set(schemes) & _OAUTH_SIMPLE_SCHEMES)
