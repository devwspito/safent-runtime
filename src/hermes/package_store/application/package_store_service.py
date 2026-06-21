"""Package Store application service — use cases consumed by D-Bus wiring.

All methods are synchronous; the D-Bus adapter runs them in an executor
for blocking search/list or fires-and-forgets threads for install/uninstall
(same pattern as SkillsHub). No I/O here — delegates to injected ports.
"""

from __future__ import annotations

import logging

from hermes.package_store.application.ports import PackageCatalogPort, PackageManagerPort
from hermes.package_store.domain.package import (
    PackageInfo,
    PackageOpStatusSnapshot,
    PackageRef,
    PackageSource,
)

logger = logging.getLogger("hermes.package_store.service")

_MAX_SEARCH_RESULTS = 50
_MAX_QUERY_LEN = 200


class PackageStoreService:
    """Application facade over catalog + manager ports."""

    def __init__(
        self,
        *,
        catalog: PackageCatalogPort,
        manager: PackageManagerPort,
    ) -> None:
        self._catalog = catalog
        self._manager = manager

    def list_installed(self, *, source: str) -> list[dict]:
        src = _parse_source(source)
        packages = self._catalog.list_installed(source=src)
        logger.info(
            "hermes.package_store.list_installed",
            extra={"source": source, "count": len(packages)},
        )
        return [_package_to_dict(p) for p in packages]

    def search(self, *, query: str, source: str) -> list[dict]:
        query = (query or "").strip()[:_MAX_QUERY_LEN]
        src = _parse_source_nullable(source)
        results = self._catalog.search(query=query, source=src)
        trimmed = results[:_MAX_SEARCH_RESULTS]
        logger.info(
            "hermes.package_store.search",
            extra={"query": query, "source": source, "hits": len(trimmed)},
        )
        return [_package_to_dict(p) for p in trimmed]

    def start_install(self, *, source: str, package_id: str) -> dict:
        ref = _build_ref(source, package_id)
        result = self._manager.start_install(ref)
        logger.info(
            "hermes.package_store.install_started",
            extra={"source": source, "package_id": package_id, "op_id": result.op_id},
        )
        return {"op_id": result.op_id}

    def start_uninstall(self, *, source: str, package_id: str) -> dict:
        ref = _build_ref(source, package_id)
        result = self._manager.start_uninstall(ref)
        logger.info(
            "hermes.package_store.uninstall_started",
            extra={"source": source, "package_id": package_id, "op_id": result.op_id},
        )
        return {"op_id": result.op_id}

    def get_op_status(self, *, op_id: str) -> dict:
        snapshot = self._manager.get_op_status((op_id or "").strip())
        return _op_status_to_dict(snapshot)


# ---------------------------------------------------------------------------
# Private helpers — pure conversion, no I/O
# ---------------------------------------------------------------------------


def _parse_source(value: str) -> PackageSource:
    try:
        return PackageSource(value)
    except ValueError:
        raise ValueError(
            f"source doit être '{PackageSource.FLATPAK}' ou '{PackageSource.RPM}'"
        )


def _parse_source_nullable(value: str) -> PackageSource | None:
    if not value or value == "all":
        return None
    return _parse_source(value)


def _build_ref(source: str, package_id: str) -> PackageRef:
    return PackageRef(source=_parse_source(source), package_id=(package_id or "").strip())


def _package_to_dict(p: PackageInfo) -> dict:
    return {
        "source": p.ref.source.value,
        "package_id": p.ref.package_id,
        "name": p.name,
        "description": p.description,
        "version_available": p.version_available,
        "version_installed": p.version_installed or "",
        "installed": p.version_installed is not None,
    }


def _op_status_to_dict(s: PackageOpStatusSnapshot) -> dict:
    return {
        "op_id": s.op_id,
        "status": s.status.value,
        "log_tail": s.log_tail,
        "error_message": s.error_message,
    }
