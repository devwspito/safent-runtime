"""TriviaCveScanner — runs trivy fs against a target path and parses JSON output.

Trivy is invoked as a subprocess (it is a CLI binary, not a Python library).
Timeout: 120 s. On any failure the scanner returns an empty list and logs the
error — the HeuristicFallbackScanner handles the "trivy absent" case instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.scan_score import Risk, Severity

logger = logging.getLogger("hermes.security_center.trivy")

_TRIVY_BIN = "/usr/bin/trivy"
_SCAN_TIMEOUT_S = 120
_TRIVY_SEVERITY = "HIGH,CRITICAL"

_TRIVY_TO_SEVERITY: dict[str, Severity] = {
    "HIGH": Severity.HIGH,
    "CRITICAL": Severity.CRITICAL,
    "MEDIUM": Severity.MEDIUM,
    "LOW": Severity.LOW,
    "UNKNOWN": Severity.LOW,
}


def trivy_available() -> bool:
    return shutil.which(_TRIVY_BIN) is not None or Path(_TRIVY_BIN).is_file()


class TriviaCveScanner:
    """Runs trivy fs on a temporary directory containing the artifact.

    For MCP servers (argv-based), we scan the resolved executable path.
    For git-cloned skills, the target.source_url directory is scanned.
    """

    name = "cve"

    async def scan(self, target: InstallTarget) -> list[Risk]:
        scan_path = self._resolve_scan_path(target)
        if scan_path is None:
            return []
        return await self._run_trivy(scan_path, target)

    def _resolve_scan_path(self, target: InstallTarget) -> str | None:
        if target.argv:
            binary = target.argv[0]
            resolved = shutil.which(binary) or binary
            return resolved if Path(resolved).exists() else None
        if target.source_url and Path(target.source_url).is_dir():
            return target.source_url
        return None

    async def _run_trivy(self, scan_path: str, target: InstallTarget) -> list[Risk]:
        cmd = [
            _TRIVY_BIN, "fs",
            "--severity", _TRIVY_SEVERITY,
            "--format", "json",
            "--quiet",
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
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.security.trivy_exec_error: %s", exc)
            return []

        if proc.returncode not in (0, 1):
            logger.warning(
                "hermes.security.trivy_nonzero rc=%s stderr=%s",
                proc.returncode, stderr[:500].decode(errors="replace"),
            )
            return []

        return self._parse_output(stdout)

    @staticmethod
    def _parse_output(raw: bytes) -> list[Risk]:
        try:
            report = json.loads(raw)
        except json.JSONDecodeError:
            return []

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
