"""ScanRecord — persisted result of a completed install scan."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.scan_score import InstallScore, Verdict


class ScanDecision(str):
    """Operator decision after reviewing a scan: ALLOWED | BLOCKED | PENDING."""
    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"
    PENDING = "PENDING"


@dataclass(slots=True)
class ScanRecord:
    """Full record of a scan run — immutable fields set at creation, mutable decision.

    id:          UUID of this scan run.
    target:      What was scanned.
    score:       Composed InstallScore including risks.
    verdict:     Derived from score (PASS/WARN/FAIL).
    decision:    Operator override (ALLOWED/BLOCKED/PENDING).
    started_at:  Scan start timestamp (UTC).
    finished_at: Scan completion timestamp (UTC).
    cached:      True if this result came from the cache.
    elapsed_ms:  Wall-clock time in ms (0 if cached).
    """

    id: UUID = field(default_factory=uuid4)
    target: InstallTarget = field(default_factory=lambda: InstallTarget(kind="unknown", identifier="unknown"))
    score: InstallScore = field(default_factory=lambda: InstallScore(value=100))
    verdict: Verdict = Verdict.PASS
    decision: str = ScanDecision.PENDING
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    finished_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    cached: bool = False
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "scan_id": str(self.id),
            "kind": self.target.kind,
            "identifier": self.target.identifier,
            "score": self.score.value,
            "verdict": self.verdict.value,
            "decision": self.decision,
            "risks": [
                {
                    "category": r.category,
                    "severity": r.severity.value,
                    "message": r.message,
                    "evidence_ref": r.evidence_ref,
                }
                for r in self.score.risks
            ],
            "cached": self.cached,
            "elapsed_ms": self.elapsed_ms,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
        }
