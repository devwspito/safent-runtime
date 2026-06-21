"""Unit tests for ScanService — mock scanners, in-memory repos."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.security_center.application.ports import IScanner, IScanHistoryRepo, IPolicyRepo
from hermes.security_center.application.scan_service import ScanBlockedError, ScanService
from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.policy import SecurityPolicy
from hermes.security_center.domain.scan_record import ScanDecision, ScanRecord
from hermes.security_center.domain.scan_score import Risk, Severity, Verdict

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _FixedScanner:
    def __init__(self, name: str, risks: list[Risk]) -> None:
        self.name = name
        self._risks = risks
        self.call_count = 0

    async def scan(self, target: InstallTarget) -> list[Risk]:
        self.call_count += 1
        return list(self._risks)


class _CrashingScanner:
    name = "cve"

    async def scan(self, target: InstallTarget) -> list[Risk]:
        raise RuntimeError("trivy exploded")


class _InMemoryScanRepo:
    def __init__(self) -> None:
        self._records: dict[UUID, ScanRecord] = {}

    def save(self, record: ScanRecord) -> None:
        self._records[record.id] = record

    def get(self, scan_id: UUID) -> ScanRecord | None:
        return self._records.get(scan_id)

    def get_by_cache_key(self, cache_key: str) -> ScanRecord | None:
        matches = [
            r for r in self._records.values()
            if r.target.cache_key == cache_key
        ]
        if not matches:
            return None
        return max(matches, key=lambda r: r.finished_at)

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


def _make_target(**kwargs: Any) -> InstallTarget:
    defaults = {"kind": "mcp_server", "identifier": "test/server"}
    return InstallTarget(**{**defaults, **kwargs})


def _make_service(
    scanners: list[Any],
    policy: SecurityPolicy | None = None,
) -> tuple[ScanService, _InMemoryScanRepo]:
    repo = _InMemoryScanRepo()
    policy_repo = _InMemoryPolicyRepo(policy)
    svc = ScanService(
        scanners=scanners,
        history_repo=repo,
        policy_repo=policy_repo,
    )
    return svc, repo


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clean_target_returns_pass():
    svc, repo = _make_service([_FixedScanner("cve", [])])
    target = _make_target()
    record = await svc.scan(target)
    assert record.verdict == Verdict.PASS
    assert record.score.value == 100
    assert len(repo._records) == 1


@pytest.mark.asyncio
async def test_critical_cve_reduces_score():
    risk = Risk(category="cve", severity=Severity.CRITICAL, message="CVE-2024-1234", evidence_ref="CVE-2024-1234")
    svc, _ = _make_service([_FixedScanner("cve", [risk])])
    record = await svc.scan(_make_target())
    # Weight cve=35, CRITICAL penalty=25. scaled = 25*35/100 = 8
    assert record.score.value == 100 - 8
    assert record.verdict == Verdict.PASS  # 92 >= 70


@pytest.mark.asyncio
async def test_multiple_highs_yield_warn():
    # Enough CRITICAL risks across all scanners to push score below 70 (WARN/FAIL).
    # cve: 5 CRITICAL × penalty=25 × weight=35/100 = 5×8 = 40 deduction
    # provenance: 2 CRITICAL × penalty=25 × weight=20/100 = 2×5 = 10 deduction
    # mcp_lint: 2 HIGH × penalty=15 × weight=30/100 = 2×4 = 8 deduction
    # signature: 1 CRITICAL × penalty=25 × weight=15/100 = 1×3 = 3 deduction
    # Total deduction ≈ 61, score ≈ 39 (FAIL)
    cve_risks = [
        Risk(category="cve", severity=Severity.CRITICAL, message=f"CVE-{i}", evidence_ref=f"CVE-{i}")
        for i in range(5)
    ]
    prov_risks = [
        Risk(category="provenance", severity=Severity.CRITICAL, message="untrusted", evidence_ref="prov:untrusted"),
        Risk(category="provenance", severity=Severity.CRITICAL, message="untrusted2", evidence_ref="prov:untrusted2"),
    ]
    mcp_risks = [
        Risk(category="mcp_lint", severity=Severity.HIGH, message="bad_tool", evidence_ref="mcp:bad"),
        Risk(category="mcp_lint", severity=Severity.HIGH, message="bad_tool2", evidence_ref="mcp:bad2"),
    ]
    sig_risks = [
        Risk(category="signature", severity=Severity.CRITICAL, message="tampered", evidence_ref="sig:mismatch"),
    ]
    policy = SecurityPolicy(
        auto_block_fail=False,  # don't raise, just record
        require_approval_warn=True,
        scanner_weights={"cve": 35, "mcp_lint": 30, "provenance": 20, "signature": 15},
    )
    svc, _ = _make_service([
        _FixedScanner("cve", cve_risks),
        _FixedScanner("provenance", prov_risks),
        _FixedScanner("mcp_lint", mcp_risks),
        _FixedScanner("signature", sig_risks),
    ], policy=policy)
    record = await svc.scan(_make_target())
    assert record.verdict in (Verdict.WARN, Verdict.FAIL)


@pytest.mark.asyncio
async def test_result_persisted_in_repo():
    svc, repo = _make_service([_FixedScanner("cve", [])])
    record = await svc.scan(_make_target())
    fetched = repo.get(record.id)
    assert fetched is not None
    assert fetched.id == record.id


# ---------------------------------------------------------------------------
# Scanner fault isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crashing_scanner_does_not_propagate():
    svc, _ = _make_service([_CrashingScanner()])
    # Should not raise — crashing scanner returns [] via fault isolation
    record = await svc.scan(_make_target())
    assert record is not None


# ---------------------------------------------------------------------------
# auto_block_fail gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_block_fail_raises_when_fail():
    many_criticals = [
        Risk(category="cve", severity=Severity.CRITICAL, message=f"CVE-{i}", evidence_ref=f"CVE-{i}")
        for i in range(10)
    ]
    policy = SecurityPolicy.default()
    svc, _ = _make_service([_FixedScanner("cve", many_criticals)], policy=policy)
    with pytest.raises(ScanBlockedError):
        await svc.scan(_make_target())


@pytest.mark.asyncio
async def test_no_block_when_auto_block_disabled():
    many_criticals = [
        Risk(category="cve", severity=Severity.CRITICAL, message=f"CVE-{i}", evidence_ref=f"CVE-{i}")
        for i in range(10)
    ]
    policy = SecurityPolicy(
        auto_block_fail=False,
        require_approval_warn=True,
        scanner_weights={"cve": 35, "mcp_lint": 30, "provenance": 20, "signature": 15},
    )
    svc, _ = _make_service([_FixedScanner("cve", many_criticals)], policy=policy)
    record = await svc.scan(_make_target())
    assert record.verdict == Verdict.FAIL  # did not raise


@pytest.mark.asyncio
async def test_cached_fail_still_blocks_on_retry():
    """Regresión: un FAIL cacheado DEBE seguir bloqueando en el reintento.

    Antes, el cache-hit devolvía el record sin re-lanzar ScanBlockedError → un
    install bloqueado se colaba dentro de la ventana de caché (bypass del gate).
    """
    many_criticals = [
        Risk(category="cve", severity=Severity.CRITICAL, message=f"CVE-{i}", evidence_ref=f"CVE-{i}")
        for i in range(10)
    ]
    scanner = _FixedScanner("cve", many_criticals)
    svc, _ = _make_service([scanner], policy=SecurityPolicy.default())
    target = _make_target()

    with pytest.raises(ScanBlockedError):
        await svc.scan(target)        # 1er intento: FAIL → bloquea (y cachea el FAIL)
    with pytest.raises(ScanBlockedError):
        await svc.scan(target)        # 2º intento (cache hit): DEBE seguir bloqueando

    # El segundo bloqueo viene de la caché, no de un re-scan.
    assert scanner.call_count == 1


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_scan_returns_cached():
    scanner = _FixedScanner("cve", [])
    svc, _ = _make_service([scanner])
    target = _make_target(sha256="abc123")
    await svc.scan(target)
    await svc.scan(target)
    # Scanner invoked only once — second call hits cache
    assert scanner.call_count == 1


@pytest.mark.asyncio
async def test_expired_cache_re_scans():
    scanner = _FixedScanner("cve", [])
    repo = _InMemoryScanRepo()
    policy_repo = _InMemoryPolicyRepo()
    svc = ScanService(scanners=[scanner], history_repo=repo, policy_repo=policy_repo)

    target = _make_target(sha256="def456")
    await svc.scan(target)

    # Manually expire the cached record
    for r in repo._records.values():
        # Set finished_at to 25h ago to exceed the 24h TTL
        object.__setattr__(r, "finished_at", datetime(2000, 1, 1, tzinfo=UTC))

    await svc.scan(target)
    assert scanner.call_count == 2


# ---------------------------------------------------------------------------
# Scanners run in parallel (no sequential ordering constraint)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_scanners_all_called():
    s1 = _FixedScanner("cve", [])
    s2 = _FixedScanner("provenance", [])
    s3 = _FixedScanner("mcp_lint", [])
    svc, _ = _make_service([s1, s2, s3])
    await svc.scan(_make_target())
    assert s1.call_count == 1
    assert s2.call_count == 1
    assert s3.call_count == 1
