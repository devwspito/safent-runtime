"""Subprocess-backed catalog adapter.

Runs `flatpak search/list` and `dnf search/list` as subprocesses and parses
their output into domain PackageInfo objects.

Security notes:
- All subprocess arguments are passed as a list (never shell=True).
- query is length-capped and stripped before reaching here.
- PackageRef.__post_init__ rejects semicolons and backticks at domain boundary.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from hermes.package_store.domain.package import PackageInfo, PackageRef, PackageSource

if TYPE_CHECKING:
    pass

logger = logging.getLogger("hermes.package_store.catalog")

_SUBPROCESS_TIMEOUT = 30  # seconds — consistent with SkillsHub pattern


class SubprocessPackageCatalog:
    """Adapter that queries flatpak and dnf via subprocess."""

    def list_installed(self, *, source: PackageSource) -> list[PackageInfo]:
        if source == PackageSource.FLATPAK:
            return _flatpak_list_installed()
        return _rpm_list_installed()

    def search(self, *, query: str, source: PackageSource | None) -> list[PackageInfo]:
        results: list[PackageInfo] = []
        if source is None or source == PackageSource.FLATPAK:
            results.extend(_flatpak_search(query))
        if source is None or source == PackageSource.RPM:
            results.extend(_rpm_search(query))
        return results


# ---------------------------------------------------------------------------
# Flatpak helpers
# ---------------------------------------------------------------------------


def _flatpak_list_installed() -> list[PackageInfo]:
    """Run `flatpak list --app --columns=application,name,version`."""
    try:
        out = _run(["flatpak", "list", "--app", "--columns=application,name,version"])
    except _SubprocessError as exc:
        logger.warning("hermes.package_store.flatpak_list_failed: %s", exc)
        return []

    packages: list[PackageInfo] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        app_id = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else app_id
        version = parts[2].strip() if len(parts) > 2 else ""
        if not app_id:
            continue
        packages.append(PackageInfo(
            ref=PackageRef(source=PackageSource.FLATPAK, package_id=app_id),
            name=name,
            description="",
            version_available=version,
            version_installed=version,
        ))
    return packages


def _flatpak_search(query: str) -> list[PackageInfo]:
    """Run `flatpak search --columns=application,name,description,version <query>`."""
    if not query:
        return []
    try:
        out = _run([
            "flatpak", "search",
            "--columns=application,name,description,version",
            query,
        ])
    except _SubprocessError as exc:
        logger.warning("hermes.package_store.flatpak_search_failed: %s", exc)
        return []

    installed_ids = {p.ref.package_id for p in _flatpak_list_installed()}
    packages: list[PackageInfo] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        app_id = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else app_id
        desc = parts[2].strip() if len(parts) > 2 else ""
        ver = parts[3].strip() if len(parts) > 3 else ""
        if not app_id:
            continue
        packages.append(PackageInfo(
            ref=PackageRef(source=PackageSource.FLATPAK, package_id=app_id),
            name=name,
            description=desc,
            version_available=ver,
            version_installed=ver if app_id in installed_ids else None,
        ))
    return packages


# ---------------------------------------------------------------------------
# RPM / dnf helpers
# ---------------------------------------------------------------------------


def _rpm_list_installed() -> list[PackageInfo]:
    """Run `dnf list installed` and filter to user-relevant packages."""
    try:
        out = _run(["dnf", "list", "installed"])
    except _SubprocessError as exc:
        logger.warning("hermes.package_store.rpm_list_failed: %s", exc)
        return []

    return _parse_dnf_list(out, installed_version_fn=lambda v: v)


def _rpm_search(query: str) -> list[PackageInfo]:
    if not query:
        return []
    try:
        out = _run(["dnf", "search", "--quiet", query])
    except _SubprocessError as exc:
        logger.warning("hermes.package_store.rpm_search_failed: %s", exc)
        return []

    return _parse_dnf_search(out)


def _parse_dnf_list(output: str, *, installed_version_fn) -> list[PackageInfo]:  # noqa: ANN001
    packages: list[PackageInfo] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("Installed Packages") or line.startswith("Available"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pkg_name = parts[0].split(".")[0]  # strip arch suffix
        version = parts[1]
        packages.append(PackageInfo(
            ref=PackageRef(source=PackageSource.RPM, package_id=pkg_name),
            name=pkg_name,
            description="",
            version_available=version,
            version_installed=installed_version_fn(version),
        ))
    return packages


def _parse_dnf_search(output: str) -> list[PackageInfo]:
    packages: list[PackageInfo] = []
    current_name = ""
    current_desc: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            if current_name:
                packages.append(_build_rpm_info(current_name, " ".join(current_desc)))
                current_name = ""
                current_desc = []
            continue
        if line.startswith("="):
            continue
        if "." in line and " : " in line:
            if current_name:
                packages.append(_build_rpm_info(current_name, " ".join(current_desc)))
                current_desc = []
            current_name = line.split(".")[0].strip()
        elif current_name and line.startswith(":"):
            current_desc.append(line[1:].strip())
        elif current_name:
            current_desc.append(line)
    if current_name:
        packages.append(_build_rpm_info(current_name, " ".join(current_desc)))
    return packages


def _build_rpm_info(pkg_name: str, description: str) -> PackageInfo:
    return PackageInfo(
        ref=PackageRef(source=PackageSource.RPM, package_id=pkg_name),
        name=pkg_name,
        description=description,
        version_available="",
        version_installed=None,
    )


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


class _SubprocessError(RuntimeError):
    pass


def _run(argv: list[str]) -> str:
    """Run argv without shell, return stdout. Raises _SubprocessError on failure."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            shell=False,
        )
        if result.returncode != 0 and not result.stdout.strip():
            raise _SubprocessError(f"{argv[0]} exited {result.returncode}: {result.stderr[:200]}")
        return result.stdout
    except subprocess.TimeoutExpired as exc:
        raise _SubprocessError(f"{argv[0]} timed out after {_SUBPROCESS_TIMEOUT}s") from exc
    except FileNotFoundError as exc:
        raise _SubprocessError(f"{argv[0]} not found") from exc
