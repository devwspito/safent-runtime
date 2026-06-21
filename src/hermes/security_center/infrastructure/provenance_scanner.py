"""ProvenanceScanner — validate source_url against trusted origin whitelist."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.policy import SecurityPolicy
from hermes.security_center.domain.scan_score import Risk, Severity

logger = logging.getLogger("hermes.security_center.provenance")

_DEFAULT_TRUSTED = frozenset({
    "github.com",
    "gitlab.com",
    "pypi.org",
    "pythonhosted.org",      # files.pythonhosted.org — pip wheel downloads
    "npmjs.com",
    "npmjs.org",             # registry.npmjs.org — the actual npm registry host
    "rubygems.org",          # gem
    "crates.io",             # cargo
    "golang.org",            # proxy.golang.org / sum.golang.org — go modules
    "ghcr.io",
    "quay.io",
    "fedoraproject.org",
})


class ProvenanceScanner:
    """Checks whether the install source comes from a trusted origin.

    Risks returned are tagged with category="provenance" so the scoring engine
    applies the correct weight.
    """

    name = "provenance"

    def __init__(self, *, trusted_orgs: frozenset[str] | None = None) -> None:
        self._trusted_orgs = trusted_orgs if trusted_orgs is not None else _DEFAULT_TRUSTED

    def with_policy(self, policy: SecurityPolicy) -> "ProvenanceScanner":
        """Return a new instance using trusted_orgs from the policy."""
        return ProvenanceScanner(trusted_orgs=policy.trusted_orgs)

    async def scan(self, target: InstallTarget) -> list[Risk]:
        if not target.source_url:
            return [Risk(
                category="provenance",
                severity=Severity.MEDIUM,
                message="No source_url provided — origin cannot be verified",
                evidence_ref="provenance:no_url",
            )]

        host = self._extract_host(target.source_url)
        if host and self._is_trusted(host):
            return []

        return [Risk(
            category="provenance",
            severity=Severity.HIGH,
            message=f"Source origin '{host or target.source_url}' is not in the trusted list",
            evidence_ref=f"provenance:untrusted:{host or 'unknown'}",
        )]

    def _extract_host(self, url: str) -> str:
        try:
            parsed = urlparse(url if "://" in url else f"https://{url}")
            return (parsed.hostname or "").lower()
        except Exception:  # noqa: BLE001
            return ""

    def _is_trusted(self, host: str) -> bool:
        return any(host == org or host.endswith(f".{org}") for org in self._trusted_orgs)
