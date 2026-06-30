"""Composition root for the production ScanService (single source of truth).

The scanners the daemon's install gate uses. Four are PURE (no I/O — provenance,
mcp_lint, signature, composio). PackageContentScanner does bounded network I/O
(fetch + statically analyze the published artifact) — this is the scanner that
closes the C2 "scan is theater" hole, so it is REQUIRED, not optional. It is
bounded (20 s HTTP timeout, capped download) and the daemon runs it on an
offloaded thread (see dbus_runtime_service._run_scan_sync). Trivy CVE is still
excluded here: it shells out with a 120 s timeout and must only run on an
explicit async path.
"""

from __future__ import annotations


def build_default_scan_service():
    """Build the production ScanService, or return None if the package is absent.

    Returns None (never raises) so callers degrade gracefully when
    hermes.security_center is not installed — the scan is additive.
    """
    try:
        from hermes.security_center.application.scan_service import ScanService
        from hermes.security_center.infrastructure.composio_allowlist import (
            ComposioAllowlistScanner,
        )
        from hermes.security_center.infrastructure.heuristic_fallback import (
            HeuristicFallbackScanner,
        )
        from hermes.security_center.infrastructure.mcp_tool_linter import McpToolLinter
        from hermes.security_center.infrastructure.package_content_scanner import (
            PackageContentScanner,
        )
        from hermes.security_center.infrastructure.skill_content_scanner import (
            SkillContentScanner,
        )
        from hermes.security_center.infrastructure.provenance_scanner import (
            ProvenanceScanner,
        )
        from hermes.security_center.infrastructure.skill_signature_check import (
            SkillSignatureCheck,
        )
        from hermes.security_center.infrastructure.sqlite_scan_repo import (
            SQLitePolicyRepo,
            SQLiteScanRepo,
        )
    except ImportError:
        return None

    # composition.py is the inline-path composition (no trivy — 120 s timeout
    # would block the daemon's event loop).  Engine is always 'heuristic' here.
    return ScanService(
        scanners=[
            PackageContentScanner(),
            SkillContentScanner(),
            HeuristicFallbackScanner(),
            ProvenanceScanner(),
            McpToolLinter(),
            SkillSignatureCheck(),
            ComposioAllowlistScanner(),
        ],
        history_repo=SQLiteScanRepo(),
        policy_repo=SQLitePolicyRepo(),
        engine="heuristic",
    )
