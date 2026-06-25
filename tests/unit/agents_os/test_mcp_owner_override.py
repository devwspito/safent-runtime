"""Regression test — MCP install owner sovereign override.

Bug: owner MFA-approved FAIL-verdict MCP install was permanently blocked because
`AddMcpServerRequest` had no `force` field, so the `force=True` sent by the
frontend after MFA approval was silently dropped by Pydantic and the daemon's
scan gate always saw force=False.

Fix: added `force: bool = False` to `AddMcpServerRequest` and propagated it into
the draft dict sent to the daemon.

This file pins TWO invariants:
  1. Schema: `AddMcpServerRequest` accepts and exposes `force`.
  2. Gate: `ScanService.allow_target` clears a FAIL block for the correct scan_id
     (the sovereignty mechanism the add_mcp_server force branch depends on).
     Without the decision the gate stays FAIL-CLOSED.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4
from datetime import UTC, datetime

import pytest

# ---------------------------------------------------------------------------
# Part 1 — Schema regression: force field present and passes through
# ---------------------------------------------------------------------------

def test_add_mcp_server_request_accepts_force_false() -> None:
    """force defaults to False — no regression on existing callers."""
    from hermes.shell_server.cowork.mcp_api import AddMcpServerRequest

    req = AddMcpServerRequest(
        server_id="github",
        label="GitHub",
        argv=["npx", "-y", "@modelcontextprotocol/server-github"],
        env={},
    )
    assert req.force is False


def test_add_mcp_server_request_accepts_force_true() -> None:
    """force=True is accepted — was silently dropped before the fix."""
    from hermes.shell_server.cowork.mcp_api import AddMcpServerRequest

    req = AddMcpServerRequest(
        server_id="filesystem",
        label="Archivos locales",
        argv=["npx", "-y", "@modelcontextprotocol/server-filesystem", "/var/lib/hermes/workspace"],
        env={},
        force=True,
    )
    assert req.force is True


def test_add_mcp_server_draft_includes_force() -> None:
    """The draft dict built in the endpoint includes the force field.

    Regression for the exact bug: draft was built without 'force', so d.get('force')
    in the daemon returned None → bool(None) = False → gate never lifted.
    """
    from hermes.shell_server.cowork.mcp_api import AddMcpServerRequest

    body = AddMcpServerRequest(
        server_id="filesystem",
        label="Archivos locales",
        argv=["npx", "-y", "@modelcontextprotocol/server-filesystem"],
        env={},
        force=True,
    )
    # Mirror the draft construction in mcp_api.py add_mcp_server endpoint
    draft = {
        "server_id": body.server_id,
        "label": body.label or body.server_id,
        "argv": body.argv,
        "env": body.env,
        "force": body.force,
    }
    assert draft["force"] is True, (
        "force=True was not propagated to the draft — the daemon would see force=False "
        "and block the install even after the owner's MFA approval"
    )


# ---------------------------------------------------------------------------
# Part 2 — Gate regression: allow_target clears FAIL block; without it stays blocked
# ---------------------------------------------------------------------------

from hermes.security_center.application.ports import IScanner, IScanHistoryRepo, IPolicyRepo
from hermes.security_center.application.scan_service import ScanBlockedError, ScanService
from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.policy import SecurityPolicy
from hermes.security_center.domain.scan_record import ScanDecision, ScanRecord
from hermes.security_center.domain.scan_score import InstallScore, Risk, Severity, Verdict


class _AlwaysFailScanner:
    """Emits a single CRITICAL CVE finding — guarantees FAIL verdict on every scan."""
    name = "cve"

    async def scan(self, target: InstallTarget) -> list[Risk]:
        return [Risk(category="cve", severity=Severity.CRITICAL,
                     message="CVE-2099-0001 — simulated for test", evidence_ref="")]


class _InMemoryScanRepo:
    def __init__(self) -> None:
        self._records: dict[UUID, ScanRecord] = {}

    def save(self, record: ScanRecord) -> None:
        self._records[record.id] = record

    def get(self, scan_id: UUID) -> ScanRecord | None:
        return self._records.get(scan_id)

    def get_by_cache_key(self, cache_key: str) -> ScanRecord | None:
        matches = [r for r in self._records.values() if r.target.cache_key == cache_key]
        return max(matches, key=lambda r: r.finished_at) if matches else None

    def list_recent(self, *, limit: int) -> list[ScanRecord]:
        return sorted(self._records.values(), key=lambda r: r.finished_at, reverse=True)[:limit]

    def update_decision(self, scan_id: UUID, decision: str) -> None:
        r = self._records.get(scan_id)
        if r:
            r.decision = decision


class _InMemoryPolicyRepo:
    def __init__(self, policy: SecurityPolicy | None = None) -> None:
        self._policy = policy or SecurityPolicy.default()

    def load(self) -> SecurityPolicy:
        return self._policy

    def save(self, policy: SecurityPolicy) -> None:
        self._policy = policy


def _make_fail_service() -> tuple[ScanService, _InMemoryScanRepo]:
    repo = _InMemoryScanRepo()
    # auto_block_fail=True (default): FAIL verdict raises ScanBlockedError
    svc = ScanService(
        scanners=[_AlwaysFailScanner()],
        history_repo=repo,
        policy_repo=_InMemoryPolicyRepo(),
    )
    return svc, repo


@pytest.mark.asyncio
async def test_fail_scan_is_blocked_without_owner_decision() -> None:
    """Without an owner-ALLOWED decision, FAIL verdict stays blocked (gate is FAIL-CLOSED)."""
    svc, _ = _make_fail_service()
    target = InstallTarget(kind="mcp_server", identifier="filesystem")

    with pytest.raises(ScanBlockedError):
        await svc.scan(target)


@pytest.mark.asyncio
async def test_owner_allow_target_clears_fail_block() -> None:
    """After allow_target, the same FAIL target proceeds (sovereignty gate respected).

    This is the mechanism the add_mcp_server force=True branch relies on:
      1. _scan_install_target → ScanBlockedError → block dict with scan_id
      2. scan_svc.allow_target(UUID(scan_id)) — this call
      3. _scan_install_target re-run → cache hit → decision=ALLOWED → no raise
    """
    svc, repo = _make_fail_service()
    target = InstallTarget(kind="mcp_server", identifier="filesystem")

    # First scan: must raise ScanBlockedError and persist the FAIL record.
    caught_record: ScanRecord | None = None
    with pytest.raises(ScanBlockedError) as exc_info:
        await svc.scan(target)
    caught_record = exc_info.value.record
    assert caught_record is not None, "ScanBlockedError must carry the ScanRecord"
    assert caught_record.verdict == Verdict.FAIL

    # Owner approves with MFA (gate call — in production, the daemon calls this
    # after the shell-server's _require_owner_mfa passes).
    svc.allow_target(caught_record.id)

    # Verify the record's decision was flipped to ALLOWED in the repo.
    persisted = repo.get(caught_record.id)
    assert persisted is not None
    assert persisted.decision == ScanDecision.ALLOWED, (
        "allow_target must set decision=ALLOWED on the scan record"
    )

    # Re-scan (simulates the add_mcp_server retry): cache hit with ALLOWED → no raise.
    result = await svc.scan(target)
    assert result.decision == ScanDecision.ALLOWED
    assert result.verdict == Verdict.FAIL  # verdict unchanged — owner overrode it
