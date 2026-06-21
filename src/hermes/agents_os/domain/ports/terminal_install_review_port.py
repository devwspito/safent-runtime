"""Port: review a terminal install-intent through the Security Center.

The TerminalSurfaceAdapter depends on this abstraction (DIP) so the domain stays
free of the security_center concretion. The infrastructure implementation wraps
the Security Center ScanService.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from hermes.agents_os.domain.terminal_install_intent import InstallIntent


@dataclass(frozen=True, slots=True)
class InstallReviewOutcome:
    """Result of reviewing a terminal install-intent.

    allowed:  False ⇒ the command MUST NOT run (verdict FAIL / scan errored).
    verdict:  "PASS" | "WARN" | "FAIL" | "ERROR".
    score:    0–100 (100 when no scanner / advisory pass).
    reason:   human-readable explanation for the agent/owner.
    scan_id:  audit correlation id, when available.
    """

    allowed: bool
    verdict: str
    score: int
    reason: str
    scan_id: str = ""


class TerminalInstallReviewPort(Protocol):
    """Reviews a detected terminal install before execution (scan→score→gate)."""

    async def review(self, intent: InstallIntent) -> InstallReviewOutcome:
        """Scan the install target. Returns the outcome; never raises for a
        normal FAIL (encodes it in allowed=False) so the adapter controls the
        rejection message. Implementations fail CLOSED on internal error."""
        ...
