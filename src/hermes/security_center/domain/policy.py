"""SecurityPolicy — operator-configurable scanning thresholds and behavior."""

from __future__ import annotations

from dataclasses import dataclass, field


# Default scanner weights (sum must equal 100).
# "content" carries the heaviest weight: it is the only scanner that inspects the
# ACTUAL bytes of the package (install hooks, exfil patterns). A finding there —
# or its inability to fetch/parse a coverable package, which it reports as a HIGH
# risk — must dominate the verdict so scanner-absence can never score a near-PASS.
_DEFAULT_WEIGHTS: dict[str, int] = {
    "content": 40,
    "cve": 20,
    "mcp_lint": 20,
    "provenance": 12,
    "signature": 8,
}

_DEFAULT_TRUSTED_ORGS: frozenset[str] = frozenset({
    "github.com",
    "gitlab.com",
    "pypi.org",
    "pythonhosted.org",
    "npmjs.com",
    "npmjs.org",
    "rubygems.org",
    "crates.io",
    "golang.org",
    "ghcr.io",
    "quay.io",
    "fedoraproject.org",
})


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    """Immutable snapshot of operator-configured security policy.

    auto_block_fail:      Reject install automatically if verdict == FAIL.
    require_approval_warn: Require explicit operator approval if verdict == WARN.
    scanner_weights:      Per-scanner contribution (must sum to 100).
    trusted_orgs:         Source-URL origins considered trusted by provenance scanner.
    """

    auto_block_fail: bool = True
    require_approval_warn: bool = True
    scanner_weights: dict[str, int] = field(
        default_factory=lambda: dict(_DEFAULT_WEIGHTS)
    )
    trusted_orgs: frozenset[str] = field(
        default_factory=lambda: frozenset(_DEFAULT_TRUSTED_ORGS)
    )

    def __post_init__(self) -> None:
        total = sum(self.scanner_weights.values())
        if total != 100:
            raise ValueError(
                f"SecurityPolicy.scanner_weights must sum to 100, got {total}"
            )

    def weight_for(self, scanner_name: str) -> int:
        return self.scanner_weights.get(scanner_name, 0)

    def to_dict(self) -> dict:
        return {
            "auto_block_fail": self.auto_block_fail,
            "require_approval_warn": self.require_approval_warn,
            "scanner_weights": dict(self.scanner_weights),
            "trusted_orgs": sorted(self.trusted_orgs),
        }

    @classmethod
    def default(cls) -> "SecurityPolicy":
        return cls()
