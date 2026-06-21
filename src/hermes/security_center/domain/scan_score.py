"""Scoring primitives — pure domain types, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Verdict(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


# Penalty applied per risk finding: severity → score deduction.
_PENALTY: dict[Severity, int] = {
    Severity.CRITICAL: 25,
    Severity.HIGH: 15,
    Severity.MEDIUM: 8,
    Severity.LOW: 3,
}


@dataclass(frozen=True, slots=True)
class Risk:
    """A single scanner finding."""

    category: str          # e.g. "cve", "provenance", "mcp_lint", "signature"
    severity: Severity
    message: str
    evidence_ref: str = ""  # CVE ID, rule name, URL, etc.

    def penalty(self) -> int:
        return _PENALTY[self.severity]


@dataclass(frozen=True, slots=True)
class InstallScore:
    """Final composed score in the range [0, 100].

    Computed by scan_service via weighted scanner results.
    Verdict thresholds: PASS ≥ 70, WARN 40–69, FAIL < 40.
    """

    value: int
    risks: tuple[Risk, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not 0 <= self.value <= 100:
            raise ValueError(f"InstallScore.value must be 0–100, got {self.value}")

    @property
    def verdict(self) -> Verdict:
        if self.value >= 70:
            return Verdict.PASS
        if self.value >= 40:
            return Verdict.WARN
        return Verdict.FAIL


def compute_verdict(score: int) -> Verdict:
    """Stateless verdict derivation — usable without constructing InstallScore."""
    if score >= 70:
        return Verdict.PASS
    if score >= 40:
        return Verdict.WARN
    return Verdict.FAIL
