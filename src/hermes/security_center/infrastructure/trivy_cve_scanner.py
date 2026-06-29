"""TriviaCveScanner — REAL CVE scan of an install target via `trivy fs`.

Trivy is a CLI binary (not a Python lib), invoked as a subprocess. The DB is
baked into the image at TRIVY_CACHE_DIR (`--skip-db-update` → offline at runtime).

How a published package is scanned (the load-bearing part — this is what makes
the gate NOT theater): we declare the target `name@version` as the SOLE dependency
of a throwaway project and run `npm install --package-lock-only` (no install, no
scripts). That resolves the EXACT tree npm would install — the package itself plus
every transitive dependency — into a package-lock.json. `trivy fs` reads that
lockfile and reports CVEs across the whole tree. (The earlier version scanned the
`npx`/`uvx` binary path — nothing useful = theater; the bug the owner flagged.)

For a local directory target (a git-cloned skill) we run `trivy fs` over its files
directly — catching any lockfiles it ships and hardcoded secrets.

FAIL-LOUD contract (security-review 2026-06-26): when the scanner is asked to
cover a coordinate but CANNOT complete the analysis — npm/pypi resolve failed, the
baked DB is missing/stale, trivy timed out, errored, or returned an unparsable
report — it emits a `cve:unanalyzable` HIGH Risk, NEVER an empty list. An empty
list means ONE thing only: trivy ran to completion and found no HIGH/CRITICAL CVE.
This closes the fail-open hole where "could not scan" was indistinguishable from
"scanned clean" (the HeuristicFallbackScanner only covers an ABSENT binary, never a
present-but-failed run, so absence-of-analysis would otherwise reach PASS).
`_compose_score` caps a `cve:unanalyzable` HIGH to ≤45 → WARN/FAIL (owner review).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.scan_score import Risk, Severity

logger = logging.getLogger("hermes.security_center.trivy")

_TRIVY_BIN = "/usr/bin/trivy"
_SCAN_TIMEOUT_S = 120
_TRIVY_SEVERITY = "HIGH,CRITICAL"
_NPM_RESOLVE_TIMEOUT_S = 90
# evidence_ref prefix the scoring layer keys on to cap an unfinished CVE scan to
# WARN/FAIL (mirrors the content scanner's "could not analyze" HIGH).
_UNANALYZABLE_REF = "cve:unanalyzable"

_TRIVY_TO_SEVERITY: dict[str, Severity] = {
    "HIGH": Severity.HIGH,
    "CRITICAL": Severity.CRITICAL,
    "MEDIUM": Severity.MEDIUM,
    "LOW": Severity.LOW,
    "UNKNOWN": Severity.LOW,
}


def trivy_available() -> bool:
    return shutil.which(_TRIVY_BIN) is not None or Path(_TRIVY_BIN).is_file()


def trivy_db_present() -> bool:
    """True iff the baked vuln DB exists in TRIVY_CACHE_DIR.

    Engine selection must gate on this, not just the binary: an image that shipped
    trivy WITHOUT a DB (build-time prewarm failed) would otherwise select engine=trivy
    and have EVERY `trivy fs --skip-db-update` fail → a systemic fail-open were it not
    for the fail-loud contract. Gating here also restores the honest 'heuristic'
    (revisión básica) label + owner-review path when the DB is genuinely absent.
    """
    cache = os.environ.get("TRIVY_CACHE_DIR") or "/var/lib/trivy"
    return (Path(cache) / "db" / "trivy.db").is_file()


class TriviaCveScanner:
    """Runs `trivy fs` against the resolved dependency tree of an install target."""

    name = "cve"

    async def scan(self, target: InstallTarget) -> list[Risk]:
        # 1) Published package (npm/pypi — an MCP server OR a packaged skill):
        #    resolve its full dependency tree and `trivy fs` it → real CVEs.
        from hermes.security_center.infrastructure.package_content_scanner import (  # noqa: PLC0415
            PackageContentScanner,
        )

        coord = PackageContentScanner.resolve_coordinate(target)
        if coord is not None:
            return await self._scan_package(coord, target)
        # 2) Local source directory (a git-cloned skill, or any on-disk artifact):
        #    `trivy fs` over its files → vulnerable deps it ships + hardcoded secrets.
        if target.source_url and Path(target.source_url).is_dir():
            risks = await self._run_trivy(target.source_url, target)
            if risks is None:
                return self._unanalyzable(f"dir:{target.source_url}")
            return risks
        # 3) Nothing fetchable/local for trivy:
        #    - A hub SKILL.md with no deps: legitimately nothing to CVE-scan → []
        #      (the content/lint scanners cover it; there is no dependency tree).
        #    - An MCP SERVER with no published package and no source dir runs code
        #      we cannot inspect at all (a local `node srv.js` / `python3 srv.py`,
        #      or an inline runner). Absence of analysis must NOT read as PASS, so
        #      emit a cve:unanalyzable HIGH → _compose_score caps it to WARN/FAIL
        #      and the owner reviews it. (security: no silent PASS for opaque MCPs)
        if "mcp" in (target.kind or "").lower():
            return self._unanalyzable("mcp:sin-paquete-publicado-inspeccionable")
        return []

    async def _scan_package(
        self, coord: tuple[str, str, str], target: InstallTarget
    ) -> list[Risk]:
        """Materialize the target's resolved dep tree, then `trivy fs` it.

        Fail-loud: a resolve failure or an inconclusive trivy run yields a
        `cve:unanalyzable` HIGH, never [] — so an unscannable package cannot PASS.
        """
        eco, name, version = coord
        spec = version.strip() or "latest"
        with tempfile.TemporaryDirectory(prefix="trivy-cve-") as tmp:
            proj = Path(tmp)
            if eco == "npm":
                if not await self._materialize_npm(proj, name, version):
                    return self._unanalyzable(
                        f"npm:{name}@{spec} — no se pudo resolver el árbol de dependencias"
                    )
            elif eco == "pypi":
                self._materialize_pypi(proj, name, version)
            else:
                return []  # ecosystem trivy does not cover here → other scanners gate
            risks = await self._run_trivy(str(proj), target)
            if risks is None:
                return self._unanalyzable(f"{eco}:{name}@{spec}")
            return risks

    @staticmethod
    def _unanalyzable(detail: str) -> list[Risk]:
        """A HIGH risk emitted when the CVE scan could NOT complete for a covered target.

        Absence-of-analysis must never read as PASS. _compose_score caps a HIGH whose
        evidence_ref starts with `cve:unanalyzable` to ≤45 → WARN/FAIL (owner review).
        """
        return [Risk(
            category="cve",
            severity=Severity.HIGH,
            message=(
                f"No se pudo completar el escaneo de CVEs ({detail}) — el paquete no es "
                f"inspeccionable por el motor CVE; requiere revisión del dueño."
            ),
            evidence_ref=_UNANALYZABLE_REF,
        )]

    @staticmethod
    async def _materialize_npm(proj: Path, name: str, version: str) -> bool:
        """Resolve `name@version`'s FULL install tree into a package-lock.json.

        Declares the target as the only dependency of a throwaway project and runs
        `npm install --package-lock-only --ignore-scripts` — npm resolves the exact
        tree it would install (target + transitive deps) WITHOUT installing anything
        or running lifecycle scripts. trivy reads that lockfile. Returns True iff the
        lockfile was produced. Network here is the daemon's (the scan runs in a
        thread outside the agent's egress jail). False ⇒ the caller emits a
        `cve:unanalyzable` HIGH (resolve failure is NOT treated as "clean").
        """
        npm = shutil.which("npm")
        if npm is None:
            logger.warning("hermes.security.trivy_npm_absent — cannot resolve %s", name)
            return False
        spec = version.strip() or "latest"
        manifest = json.dumps({
            "name": "lumen-cve-probe",
            "version": "0.0.0",
            "private": True,
            "dependencies": {name: spec},
        })
        (proj / "package.json").write_text(manifest, encoding="utf-8")
        # ISOLATED npm cache inside the scan's temp dir. The daemon's SHARED cache
        # (npm_config_cache=/var/lib/hermes/npm-cache) can hold _cacache entries owned
        # by another uid (baked at build / written by the prefetch) → npm can't lock
        # them → EACCES → no lockfile → a legit package would WARN as "unanalyzable".
        # A fresh per-scan cache is always writable by this process and isolates the
        # probe from cross-scan/prefetch contention. `--package-lock-only` fetches only
        # packuments (no tarballs), so re-fetching per scan is cheap.
        env = dict(os.environ)
        env["npm_config_cache"] = str(proj / ".npm-cache")
        env["npm_config_update_notifier"] = "false"
        env["npm_config_fund"] = "false"
        try:
            p = await asyncio.create_subprocess_exec(
                npm, "install", "--package-lock-only", "--ignore-scripts",
                "--no-audit", "--no-fund", "--loglevel=error",
                cwd=str(proj), env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                p.communicate(), timeout=_NPM_RESOLVE_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001 (incl. TimeoutError)
            logger.warning(
                "hermes.security.trivy_npm_resolve_failed %s@%s: %s", name, spec, exc
            )
            return False
        if not (proj / "package-lock.json").is_file():
            logger.warning(
                "hermes.security.trivy_npm_no_lockfile %s@%s stderr=%s",
                name, spec, stderr[:300].decode(errors="replace") if stderr else "",
            )
            return False
        return True

    @staticmethod
    def _materialize_pypi(proj: Path, name: str, version: str) -> None:
        """Pin `name==version` into requirements.txt for trivy's pip analyzer.

        trivy reads requirements.txt and matches the pinned package against the vuln
        DB — catching the target package's own CVEs. (Transitive pypi deps are not
        pinned here; the target's own CVEs are the primary signal for an install.)
        """
        line = f"{name}=={version.strip()}" if version.strip() else name
        (proj / "requirements.txt").write_text(line + "\n", encoding="utf-8")

    async def _run_trivy(
        self, scan_path: str, target: InstallTarget
    ) -> list[Risk] | None:
        """Run `trivy fs` and parse it.

        Returns a (possibly empty) list when trivy RAN to completion — [] then means
        "scanned, no HIGH/CRITICAL CVE". Returns None when the scan could NOT complete
        (timeout, exec error, non-zero exit incl. a missing/stale baked DB, or an
        unparsable report) — the caller turns None into a `cve:unanalyzable` HIGH so
        the inconclusive scan can never read as a clean PASS.
        """
        # --skip-db-update: use the DB baked into the image at TRIVY_CACHE_DIR. A
        # missing/stale DB makes trivy exit non-zero → None → unanalyzable (NOT clean).
        cmd = [
            _TRIVY_BIN, "fs",
            "--severity", _TRIVY_SEVERITY,
            "--format", "json",
            "--quiet",
            "--skip-db-update",
            scan_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_SCAN_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            logger.warning(
                "hermes.security.trivy_timeout kind=%s path=%s",
                target.kind, scan_path,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.security.trivy_exec_error: %s", exc)
            return None

        if proc.returncode not in (0, 1):
            logger.warning(
                "hermes.security.trivy_nonzero rc=%s stderr=%s",
                proc.returncode, stderr[:500].decode(errors="replace"),
            )
            return None

        return self._parse_output(stdout)

    @staticmethod
    def _parse_output(raw: bytes) -> list[Risk] | None:
        """Parse trivy JSON into Risks. None ⇒ unparsable/empty ⇒ scan inconclusive."""
        if not raw or not raw.strip():
            return None
        try:
            report = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(report, dict):
            return None

        risks: list[Risk] = []
        for result in report.get("Results") or []:
            for vuln in result.get("Vulnerabilities") or []:
                sev_str = (vuln.get("Severity") or "UNKNOWN").upper()
                severity = _TRIVY_TO_SEVERITY.get(sev_str, Severity.LOW)
                cve_id = vuln.get("VulnerabilityID") or "UNKNOWN"
                pkg = vuln.get("PkgName") or ""
                risks.append(Risk(
                    category="cve",
                    severity=severity,
                    message=f"{cve_id} in {pkg}",
                    evidence_ref=cve_id,
                ))
        return risks
