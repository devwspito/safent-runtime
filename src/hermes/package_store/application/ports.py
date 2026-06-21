"""Ports (interfaces) for the Package Store application layer.

Infrastructure adapters implement these; application use-cases depend on them.
"""

from __future__ import annotations

from typing import Protocol

from hermes.package_store.domain.package import (
    PackageInfo,
    PackageOpResult,
    PackageOpStatus,
    PackageOpStatusSnapshot,
    PackageRef,
    PackageSource,
)


class PackageCatalogPort(Protocol):
    """Read-only access to what is available and what is installed."""

    def list_installed(self, *, source: PackageSource) -> list[PackageInfo]: ...

    def search(self, *, query: str, source: PackageSource | None) -> list[PackageInfo]: ...


class PackageManagerPort(Protocol):
    """Mutating operations — install and uninstall."""

    def start_install(self, ref: PackageRef) -> PackageOpResult: ...

    def start_uninstall(self, ref: PackageRef) -> PackageOpResult: ...

    def get_op_status(self, op_id: str) -> PackageOpStatusSnapshot: ...
