"""Unit tests for individual scanner implementations."""

from __future__ import annotations

import json

import pytest

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.scan_score import Severity
from hermes.security_center.infrastructure.composio_allowlist import ComposioAllowlistScanner
from hermes.security_center.infrastructure.heuristic_fallback import HeuristicFallbackScanner
from hermes.security_center.infrastructure.mcp_tool_linter import McpToolLinter
from hermes.security_center.infrastructure.provenance_scanner import ProvenanceScanner

pytestmark = pytest.mark.unit


def _target(**kwargs) -> InstallTarget:
    base = {"kind": "mcp_server", "identifier": "test"}
    return InstallTarget(**{**base, **kwargs})


# ---------------------------------------------------------------------------
# ProvenanceScanner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provenance_trusted_github():
    scanner = ProvenanceScanner()
    risks = await scanner.scan(_target(source_url="https://github.com/user/repo"))
    assert risks == []


@pytest.mark.asyncio
async def test_provenance_trusted_subdomain():
    scanner = ProvenanceScanner()
    # raw.github.com IS a subdomain of github.com — should pass.
    # Note: raw.githubusercontent.com is a sibling domain, NOT a subdomain of github.com.
    risks = await scanner.scan(_target(source_url="https://raw.github.com/user/repo"))
    assert risks == []


@pytest.mark.asyncio
async def test_provenance_untrusted_returns_high():
    scanner = ProvenanceScanner()
    risks = await scanner.scan(_target(source_url="https://evil.example.com/payload"))
    assert len(risks) == 1
    assert risks[0].severity == Severity.HIGH
    assert risks[0].category == "provenance"


@pytest.mark.asyncio
async def test_provenance_no_url_returns_medium():
    scanner = ProvenanceScanner()
    risks = await scanner.scan(_target(source_url=""))
    assert len(risks) == 1
    assert risks[0].severity == Severity.MEDIUM


# ---------------------------------------------------------------------------
# McpToolLinter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_linter_clean_manifest():
    manifest = json.dumps({"tools": [{"name": "read_file"}, {"name": "list_directory"}]})
    scanner = McpToolLinter()
    risks = await scanner.scan(_target(manifest_json=manifest))
    assert risks == []


@pytest.mark.asyncio
async def test_mcp_linter_destructive_tool():
    manifest = json.dumps({"tools": [{"name": "delete_user"}, {"name": "read_config"}]})
    scanner = McpToolLinter()
    risks = await scanner.scan(_target(manifest_json=manifest))
    assert any("delete_user" in r.message for r in risks)
    assert all(r.category == "mcp_lint" for r in risks)


@pytest.mark.asyncio
async def test_mcp_linter_drop_tool():
    manifest = json.dumps({"tools": [{"name": "drop_table"}]})
    scanner = McpToolLinter()
    risks = await scanner.scan(_target(manifest_json=manifest))
    assert len(risks) == 1
    assert risks[0].severity == Severity.HIGH


@pytest.mark.asyncio
async def test_mcp_linter_risky_runner():
    scanner = McpToolLinter()
    risks = await scanner.scan(_target(argv=["bash", "-c", "do_something"]))
    assert len(risks) == 1
    assert "bash" in risks[0].message
    assert risks[0].severity == Severity.HIGH


@pytest.mark.asyncio
async def test_mcp_linter_safe_runner():
    scanner = McpToolLinter()
    risks = await scanner.scan(_target(argv=["npx", "@company/mcp-server"]))
    assert risks == []


@pytest.mark.asyncio
async def test_mcp_linter_invalid_manifest_returns_low_risk():
    scanner = McpToolLinter()
    risks = await scanner.scan(_target(manifest_json="this is not json"))
    assert len(risks) == 1
    assert risks[0].severity == Severity.LOW


@pytest.mark.asyncio
async def test_mcp_linter_no_manifest_returns_empty():
    scanner = McpToolLinter()
    risks = await scanner.scan(_target(manifest_json=""))
    assert risks == []


# ---------------------------------------------------------------------------
# ComposioAllowlistScanner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_composio_always_passes():
    scanner = ComposioAllowlistScanner()
    risks = await scanner.scan(_target(kind="composio_app", identifier="github"))
    assert risks == []


# ---------------------------------------------------------------------------
# HeuristicFallbackScanner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heuristic_fallback_returns_medium_cve_risk():
    scanner = HeuristicFallbackScanner()
    risks = await scanner.scan(_target())
    assert len(risks) == 1
    assert risks[0].category == "cve"
    assert risks[0].severity == Severity.MEDIUM
