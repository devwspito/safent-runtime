"""Domain value objects for the Package Store bounded context.

Pure Python — no subprocess, no HTTP, no framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class PackageSource(StrEnum):
    FLATPAK = "flatpak"
    RPM = "rpm"


class PackageOpStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PackageRef:
    """Uniquely identifies a package across sources.

    flatpak: package_id is the application ID (e.g. org.inkscape.Inkscape)
    rpm:     package_id is the dnf package name (e.g. gimp)
    """

    source: PackageSource
    package_id: str

    def __post_init__(self) -> None:
        if not self.package_id or not self.package_id.strip():
            raise ValueError("package_id must not be blank")
        if ";" in self.package_id or "`" in self.package_id:
            raise ValueError("package_id contains forbidden characters")


@dataclass(frozen=True, slots=True)
class PackageInfo:
    """Snapshot of a package listing result."""

    ref: PackageRef
    name: str
    description: str
    version_available: str
    version_installed: str | None  # None means not installed


@dataclass(frozen=True, slots=True)
class PackageOpResult:
    """Result returned when starting an async install/uninstall operation."""

    op_id: str
    ref: PackageRef


@dataclass(frozen=True, slots=True)
class PackageOpStatusSnapshot:
    """Polled status of a running install/uninstall operation."""

    op_id: str
    status: PackageOpStatus
    log_tail: str = ""
    error_message: str = ""
