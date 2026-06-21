"""ScanService — orchestrates parallel scanners, composes weighted score, persists ScanRecord."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from uuid import UUID

from hermes.security_center.application.ports import IScanner, IScanHistoryRepo, IPolicyRepo
from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.policy import SecurityPolicy
from hermes.security_center.domain.scan_record import ScanDecision, ScanRecord
from hermes.security_center.domain.scan_score import InstallScore, Risk, Severity, Verdict

logger = logging.getLogger("hermes.security_center.scan_service")

# Cache TTLs in seconds.
_TTL_WITH_SHA256 = 86_400   # 24 h
_TTL_WITHOUT_SHA256 = 3_600  # 1 h


class ScanBlockedError(RuntimeError):
    """Raised when auto_block_fail=True and scan verdict is FAIL."""


class ScanService:
    """Orchestrates all scanners in parallel and produces a single ScanRecord.

    Flow:
      1. Check cache by (kind, sha256) or (kind, identifier).
      2. Run enabled scanners concurrently via asyncio.gather.
      3. Compose weighted score: start at 100, apply per-scanner penalties.
      4. Persist ScanRecord and return it.
    """

    def __init__(
        self,
        *,
        scanners: list[IScanner],
        history_repo: IScanHistoryRepo,
        policy_repo: IPolicyRepo,
    ) -> None:
        self._scanners = scanners
        self._history_repo = history_repo
        self._policy_repo = policy_repo

    async def scan(self, target: InstallTarget) -> ScanRecord:
        """Run the full scan pipeline. Returns the final ScanRecord.

        Raises ScanBlockedError if policy.auto_block_fail and verdict == FAIL.
        """
        policy = self._policy_repo.load()

        cached = self._check_cache(target, policy)
        if cached is not None:
            logger.info(
                "hermes.security.cache_hit cache_key=%s scan_id=%s",
                target.cache_key, cached.id,
            )
            # El cache DEBE aplicar el mismo gate que un scan fresco: si el
            # veredicto cacheado es FAIL con auto_block, re-lanzar el bloqueo. Sin
            # esto, un install bloqueado se cuela en el reintento dentro de la
            # ventana de caché (bypass fail-open del gate).
            # Owner sovereignty (modelo "nada prohibido, todo elevable"): si el dueño
            # revisó este scan y marcó decision=ALLOWED, es una aprobación PERMANENTE
            # de ESTE target — el gate la respeta aunque el veredicto sea FAIL. Es la
            # vía soberana para instalar herramientas legítimas pero potentes (p.ej.
            # un harness de agentes que declara hooks/exec) que el antivirus marca.
            if cached.decision == ScanDecision.ALLOWED:
                logger.warning(
                    "hermes.security.owner_override_allowed kind=%s identifier=%s "
                    "verdict=%s — instalación permitida por decisión SOBERANA del dueño",
                    target.kind, target.identifier, cached.verdict.value,
                )
                return cached
            if policy.auto_block_fail and cached.verdict == Verdict.FAIL:
                raise ScanBlockedError(
                    f"Install blocked (cached): scan verdict FAIL "
                    f"for {target.kind}:{target.identifier}"
                )
            return cached

        started_at = datetime.now(tz=UTC)
        t0 = time.monotonic()

        all_risks = await self._run_scanners(target, policy)
        score_value = self._compose_score(all_risks, policy)
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        install_score = InstallScore(value=score_value, risks=tuple(all_risks))
        record = ScanRecord(
            target=target,
            score=install_score,
            verdict=install_score.verdict,
            decision=ScanDecision.PENDING,
            started_at=started_at,
            finished_at=datetime.now(tz=UTC),
            cached=False,
            elapsed_ms=elapsed_ms,
        )

        # Decidir el bloqueo ANTES de cualquier I/O falible (fail-closed): un fallo
        # al persistir el ScanRecord NO debe suprimir un veredicto FAIL. La
        # persistencia es best-effort (historial/auditoría), no parte del gate.
        must_block = policy.auto_block_fail and record.verdict == Verdict.FAIL

        try:
            self._history_repo.save(record)
        except Exception as exc:  # noqa: BLE001 — persistir es best-effort, no debe tumbar el gate
            logger.error(
                "hermes.security.scan_persist_failed scan_id=%s: %s", record.id, exc
            )

        logger.info(
            "hermes.security.scan_complete scan_id=%s kind=%s score=%d verdict=%s elapsed_ms=%d",
            record.id, target.kind, score_value, record.verdict.value, elapsed_ms,
        )

        if must_block:
            raise ScanBlockedError(
                f"Install blocked: scan verdict FAIL (score={score_value}) "
                f"for {target.kind}:{target.identifier}"
            )

        return record

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_cache(self, target: InstallTarget, policy: SecurityPolicy) -> ScanRecord | None:
        cached = self._history_repo.get_by_cache_key(target.cache_key)
        if cached is None:
            return None
        # Una aprobación SOBERANA del dueño (decision=ALLOWED) es permanente para ese
        # target: no caduca con el TTL del scan (que solo gobierna el re-análisis).
        if cached.decision == ScanDecision.ALLOWED:
            return cached
        ttl = _TTL_WITH_SHA256 if target.has_sha256 else _TTL_WITHOUT_SHA256
        age_s = (datetime.now(tz=UTC) - cached.finished_at).total_seconds()
        if age_s > ttl:
            return None
        return cached

    # Scanners that perform REAL content analysis (read the package bytes). They
    # must run regardless of policy weight: their findings — including the
    # "could not analyze" HIGH — feed the non-negotiable score caps in
    # _compose_score, so a weight of 0 must never silently disable them. Letting
    # an operator zero-out "content" would re-open the C2 hole (no analysis ⇒
    # PASS) via policy instead of runner.
    _ALWAYS_ON_SCANNERS: frozenset[str] = frozenset({"content"})

    async def _run_scanners(
        self, target: InstallTarget, policy: SecurityPolicy
    ) -> list[Risk]:
        tasks = [
            self._run_one_scanner(scanner, target)
            for scanner in self._scanners
            if policy.weight_for(scanner.name) > 0
            or scanner.name in self._ALWAYS_ON_SCANNERS
        ]
        results = await asyncio.gather(*tasks)
        return [risk for scanner_risks in results for risk in scanner_risks]

    @staticmethod
    async def _run_one_scanner(scanner: IScanner, target: InstallTarget) -> list[Risk]:
        try:
            return await scanner.scan(target)
        except Exception as exc:  # noqa: BLE001 — scanner must never crash the pipeline
            logger.warning(
                "hermes.security.scanner_error scanner=%s: %s", scanner.name, exc
            )
            return []

    def _compose_score(self, risks: list[Risk], policy: SecurityPolicy) -> int:
        """Weighted deduction from 100, with hard caps for decisive findings.

        The plain weighted model dilutes severe findings: a single CRITICAL
        (penalty 25) at weight 40 only deducts 10, leaving an exfil package at
        ~90 → PASS. That is exactly the C2 "scan is theater" hole. So before the
        weighted sum we apply non-negotiable caps:

          - ANY CRITICAL finding  → score forced to FAIL (< 40). A package that
            runs an install hook or fails signature verification is never PASS.
          - ANY HIGH finding from a CONTENT analysis (the only scanner that read
            the real bytes — including its "could not analyze" signal) → score
            capped into WARN/FAIL territory so it can never auto-allow.

        The weighted sum still runs for the residual LOW/MEDIUM noise and for
        non-content HIGHs, so benign packages keep a meaningful gradient.
        """
        from hermes.security_center.domain.scan_score import Severity

        total_deduction = 0
        for risk in risks:
            weight = policy.weight_for(risk.category)
            scaled_penalty = int(risk.penalty() * weight / 100)
            total_deduction += scaled_penalty
        score = max(0, min(100, 100 - total_deduction))

        has_critical = any(r.severity == Severity.CRITICAL for r in risks)
        content_high = any(
            r.severity == Severity.HIGH and r.category == "content" for r in risks
        )
        if has_critical:
            return min(score, 30)   # below the FAIL threshold (40)
        if content_high:
            return min(score, 45)   # WARN at best — never an auto-allow PASS
        return score
