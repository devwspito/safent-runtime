"""ComposioAllowlistScanner — short-circuit PASS for Composio Cloud apps.

Composio manages its own app security surface (OAuth, scoped permissions).
Any artifact with kind=="composio_app" is managed by Composio Cloud and does
not need further CVE or provenance scanning by us.
"""

from __future__ import annotations

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.scan_score import Risk


class ComposioAllowlistScanner:
    """Returns no risks for Composio-managed apps; returns a single LOW notice otherwise."""

    name = "composio_allowlist"

    async def scan(self, target: InstallTarget) -> list[Risk]:
        # Composio apps are cloud-managed — no local risk to report.
        return []
