"""HeuristicFallbackScanner — caps score to 70 when trivy is unavailable.

This scanner does not produce risks — instead, it emits a single LOW/MEDIUM
notice that the CVE scanner was skipped. The scan_service respects this by
capping the effective CVE contribution, ensuring the final score never reaches
100 when CVE data is missing.
"""

from __future__ import annotations

import logging

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.scan_score import Risk, Severity

logger = logging.getLogger("hermes.security_center.heuristic_fallback")


class HeuristicFallbackScanner:
    """Injected in place of TriviaCveScanner when the trivy binary is absent.

    Emits a single MEDIUM risk under category="cve" so that the weighted
    score engine deducts the cve-weight fraction, effectively capping the
    score at ≤70 (the PASS threshold boundary).
    """

    name = "cve"

    async def scan(self, target: InstallTarget) -> list[Risk]:  # noqa: ARG002
        logger.warning(
            "hermes.security.trivy_unavailable kind=%s identifier=%s — "
            "CVE scan skipped, score capped",
            target.kind, target.identifier,
        )
        return [Risk(
            category="cve",
            severity=Severity.MEDIUM,
            message="CVE scan unavailable — trivy binary not found at /usr/bin/trivy",
            evidence_ref="cve:trivy_missing",
        )]
