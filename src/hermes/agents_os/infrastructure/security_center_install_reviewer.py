"""SecurityCenterInstallReviewer — bridges a terminal install-intent to the
Security Center ScanService (TerminalInstallReviewPort implementation).

Maps an InstallIntent → InstallTarget(kind="package") → scan → InstallReviewOutcome.
Fails CLOSED: if the scanner errors (deployed but crashed), the install is denied.
If the Security Center is not installed at all, the reviewer is simply not wired
(the adapter receives None) and terminal installs fall back to the egress jail +
broker HITL — never silently "pass" because a scanner blew up.
"""

from __future__ import annotations

import logging

from hermes.agents_os.domain.ports.terminal_install_review_port import (
    InstallReviewOutcome,
    TerminalInstallReviewPort,
)
from hermes.agents_os.domain.terminal_install_intent import InstallIntent

logger = logging.getLogger(__name__)


class SecurityCenterInstallReviewer(TerminalInstallReviewPort):
    """Reviews terminal installs via the Security Center scan→score→gate."""

    def __init__(self, scan_service) -> None:  # ScanService
        self._scan = scan_service

    async def review(self, intent: InstallIntent) -> InstallReviewOutcome:
        from hermes.security_center.application.scan_service import ScanBlockedError
        from hermes.security_center.domain.install_target import InstallTarget

        target = InstallTarget(
            kind="package",
            identifier=f"{intent.ecosystem}:{intent.identifier}",
            source_url=intent.source_url,
        )
        try:
            record = await self._scan.scan(target)
        except ScanBlockedError as exc:
            logger.warning(
                "hermes.terminal.install_blocked ecosystem=%s id=%s: %s",
                intent.ecosystem, intent.identifier, exc,
            )
            return InstallReviewOutcome(
                allowed=False, verdict="FAIL", score=0,
                reason=f"Centro de Seguridad bloqueó el install ({intent.ecosystem}:{intent.identifier}): {exc}",
            )
        except Exception as exc:  # noqa: BLE001 — scanner deployed but crashed → fail-closed
            logger.error(
                "hermes.terminal.install_scan_errored ecosystem=%s id=%s: %s (fail-closed)",
                intent.ecosystem, intent.identifier, exc,
            )
            return InstallReviewOutcome(
                allowed=False, verdict="ERROR", score=0,
                reason="No se pudo verificar la seguridad del install (el análisis falló) — denegado por seguridad.",
            )

        from hermes.security_center.domain.scan_score import Severity

        verdict = record.verdict.value if hasattr(record.verdict, "value") else str(record.verdict)
        score = int(getattr(record.score, "value", 100) or 0)
        risks = list(getattr(record.score, "risks", []) or [])
        high = [
            r for r in risks
            if getattr(r, "severity", None) in (Severity.HIGH, Severity.CRITICAL)
        ]
        # Security-First for the terminal side-door (stricter than the official UI
        # channel): auto-allow ONLY a clean PASS with no HIGH/CRITICAL finding. An
        # untrusted source (provenance HIGH) or any WARN → blocked; the owner elevates
        # the source (trusted-orgs / egress allow-list) to permit it deliberately.
        allowed = verdict == "PASS" and not high
        if allowed:
            reason = f"scan PASS (score={score}) para {intent.ecosystem}:{intent.identifier}"
        else:
            findings = "; ".join(
                f"{getattr(r, 'category', '?')}:{getattr(r, 'severity', '?')}" for r in (high or risks)
            ) or verdict
            reason = (
                f"Centro de Seguridad NO aprueba {intent.ecosystem}:{intent.identifier} "
                f"(verdict={verdict}, score={score}, hallazgos: {findings}). "
                f"Elevá la fuente para permitirlo."
            )
        return InstallReviewOutcome(
            allowed=allowed, verdict=verdict, score=score, reason=reason,
            scan_id=str(getattr(record, "id", "")),
        )
