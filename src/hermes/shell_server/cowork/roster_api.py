"""Agent roster endpoint — GET /api/v1/agents/roster.

Devuelve el equipo de agentes agrupado en departamentos, TODO desde el registro de
agentes del daemon (agentes reales, no un catálogo externo):
  • "cerebro"      — el agente default (is_default=True), el que orquesta.
  • Factory        — el roster de especialistas sembrado (default_roster), por departamento.
  • Custom depts   — agentes custom con un department explícito.
  • "mis-agentes"  — agentes custom sin department.

No hay catálogo externo ni harness: el equipo ES el registro real, ejecutado por el
Cerebro vía delegación nativa. Read-only, sin auth (misma postura que GET /api/v1/agents).

Fase 3 (department-scoped visibility): when the cloud has pushed a directory
(AccessScopeSpec.visibility_scope != "all"), the bound employee's visible
colleague agents are SURFACED alongside the local roster, grouped by their
own `department`. No directory stored (the default, visibility_scope="all")
-> local roster only, byte-for-byte today's behaviour (zero regression). The
directory is read-only presentation data; the cloud already enforces the
delegation department-gate authoritatively — see DelegationSurfaceAdapter.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

from hermes.agents.domain.default_roster import DEPARTMENTS

if TYPE_CHECKING:
    from hermes.shell_server.security.secrets import SecretsVault

logger = logging.getLogger("hermes.shell_server.cowork.roster_api")


def _agent_shape(a: dict[str, Any]) -> dict[str, Any]:
    dept = a.get("department")
    is_factory = bool(dept) and dept in DEPARTMENTS
    return {
        "id": a.get("agent_id", ""),
        "name": a.get("name", ""),
        "description": a.get("primary_mission", ""),
        "department": dept,
        "is_default": bool(a.get("is_default", False)),
        "color": a.get("color") or None,
        "source": "factory" if is_factory else "custom",
    }


def _directory_entry_shape(entry: dict[str, Any]) -> dict[str, Any]:
    """Shape one delivered DirectoryEntrySpec dict as a read-only colleague card.

    Never locally runnable — `source: "directory"` tells the frontend to
    render it without edit/delete/chat affordances (it belongs to a
    colleague's own instance, not this one).
    """
    return {
        "id": entry.get("agent_id", ""),
        "name": entry.get("name", ""),
        "description": "",
        "department": entry.get("department") or None,
        "is_default": False,
        "color": None,
        "source": "directory",
    }


def _bucket_by_department(
    shape: dict[str, Any],
    by_dept: dict[str, list[dict[str, Any]]],
    misc: list[dict[str, Any]],
) -> None:
    """Files a shaped agent card under its department bucket, or `misc`."""
    dept = (shape["department"] or "").strip()
    if dept:
        by_dept.setdefault(dept, []).append(shape)
    else:
        misc.append(shape)


def _merge_directory_entries(
    directory_entries: list[dict[str, Any]] | None,
    *,
    local_ids: set[str],
    by_dept: dict[str, list[dict[str, Any]]],
    misc: list[dict[str, Any]],
) -> None:
    """Surfaces directory-sourced colleagues, skipping any local_ids collision."""
    for entry in directory_entries or []:
        shape = _directory_entry_shape(entry)
        if shape["id"] in local_ids:
            continue  # already surfaced from the local registry; avoid duplicates
        _bucket_by_department(shape, by_dept, misc)


def _build_departments(
    agents: list[dict[str, Any]],
    directory_entries: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Agrupa los agentes del registro (+ directorio Fase 3, si lo hay) en departamentos."""
    cerebro: list[dict[str, Any]] = []
    by_dept: dict[str, list[dict[str, Any]]] = {}
    misc: list[dict[str, Any]] = []
    local_ids: set[str] = set()

    for a in agents:
        shape = _agent_shape(a)
        local_ids.add(shape["id"])
        if a.get("is_default"):
            shape["department"] = "cerebro"
            cerebro.append(shape)
            continue
        _bucket_by_department(shape, by_dept, misc)

    _merge_directory_entries(directory_entries, local_ids=local_ids, by_dept=by_dept, misc=misc)

    departments: list[dict[str, Any]] = []

    # CEO always first.
    if cerebro:
        departments.append(
            {"id": "cerebro", "name": "CEO", "kind": "cerebro", "agents": cerebro}
        )

    # Departamentos de fábrica primero, en el orden de DEPARTMENTS.
    for slug, (label, _color) in DEPARTMENTS.items():
        bucket = by_dept.pop(slug, None)
        if bucket:
            departments.append(
                {"id": slug, "name": label, "kind": "factory", "agents": bucket}
            )

    # Departamentos custom del usuario (alfabético).
    for slug in sorted(by_dept):
        departments.append(
            {
                "id": f"custom:{slug}",
                "name": slug.replace("-", " ").replace("_", " ").title(),
                "kind": "custom",
                "agents": by_dept[slug],
            }
        )

    # Agentes custom sin departamento.
    if misc:
        departments.append(
            {"id": "mis-agentes", "name": "Mis agentes", "kind": "custom", "agents": misc}
        )

    return departments


def _load_directory_entries(
    db_path: Path, vault: "SecretsVault"
) -> list[dict[str, Any]] | None:
    """Fail-soft read of the Fase-3 directory persisted by config_sync.

    Returns None when no directory has been delivered (visibility_scope=
    "all", the default, or the instance isn't paired) — the caller must then
    behave EXACTLY as before Fase 3 (local roster only, zero regression).
    """
    try:
        from hermes.instance.association_store import SQLiteAssociationStore  # noqa: PLC0415

        store = SQLiteAssociationStore(db_path=db_path, vault=vault)
        assoc = store.get()
    except Exception:  # noqa: BLE001
        logger.warning("hermes.roster.directory_read_failed", exc_info=True)
        return None
    if assoc is None or assoc.directory is None:
        return None
    entries = assoc.directory.get("entries", [])
    return entries if isinstance(entries, list) else None


def create_roster_router(db_path: Path, vault: "SecretsVault") -> APIRouter:
    """Return the APIRouter for GET /api/v1/agents/roster."""
    router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

    @router.get("/roster")
    async def get_agent_roster(request: Request) -> dict[str, Any]:
        """Equipo de agentes agrupado en departamentos (registro real + directorio Fase 3).

        Fail-soft: si el daemon no responde, devuelve departamentos vacíos. Nunca 500.
        """
        proxy = request.app.state.dbus_proxy
        try:
            raw_agents: list[dict] = await proxy.call_list("list_agents")
        except Exception:  # noqa: BLE001
            raw_agents = []

        directory_entries = _load_directory_entries(db_path, vault)
        departments = _build_departments(raw_agents, directory_entries)
        logger.debug("hermes.roster.built departments=%d", len(departments))
        return {"departments": departments}

    return router
