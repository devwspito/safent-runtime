"""Shared MFA-tier verifier — the ONE place that answers "what MFA does approving this
tool require, and are these factors valid?".

Before this, the MFA check lived only in the web layer (approvals_api). The D-Bus
Approve path reached gate.approve WITHOUT any MFA → a real side-door (red-team
2026-06-19, finding 3). Now BOTH surfaces forward raw factors to gate.approve, which
verifies through this single verifier, so an approve without valid owner MFA is
impossible regardless of surface.

Layering: this lives in shell_server/security (next to MfaStore). It is INJECTED into
the SqliteApprovalGate at wiring time (the gate duck-types it via a `mfa_verifier` port),
so capabilities/infrastructure never imports the presentation layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.capabilities.tool_delicacy import Delicacy, delicacy
from hermes.shell_server.security.mfa import MfaStore, ProtectionLevel

# Delicacy → MFA tier. Single source (was duplicated in approvals_api._DELICACY_TO_MFA).
_DELICACY_TO_MFA: dict[Delicacy, ProtectionLevel] = {
    Delicacy.NORMAL: ProtectionLevel.MFA,
    Delicacy.DELICATE: ProtectionLevel.MFA_HUMANITY,
    Delicacy.MOST_DELICATE: ProtectionLevel.MFA_RIDDLE,
}


def classify_level(risk: str, tool: str) -> ProtectionLevel:
    """Server-side MFA tier from the single delicacy source (never trust the client).

    A 'critical'/'severe' daemon risk floors the tier at MFA+humanity even for a tool
    the catalog rates NORMAL.
    """
    level = _DELICACY_TO_MFA[delicacy(tool)]
    if level is ProtectionLevel.MFA and (risk or "").lower() in ("critical", "severe"):
        return ProtectionLevel.MFA_HUMANITY
    return level


@dataclass(frozen=True)
class MfaFactors:
    """Raw owner-supplied factors, forwarded from any approve surface to the gate."""

    totp: str | None = None
    humanity: str | None = None
    riddle_answer: str | None = None


class MfaToolTierVerifier:
    """Verifies owner MFA for approving a tool, at the tier its delicacy demands.

    Injected into SqliteApprovalGate so the gate (not just the web layer) enforces MFA.
    """

    def __init__(self, store: MfaStore | None = None) -> None:
        self._store = store or MfaStore()

    def verify_for_tool(
        self, *, tool_name: str, risk: str, factors: MfaFactors | None
    ) -> tuple[bool, str]:
        """(ok, reason). Fail-closed: not enrolled or no factors → reject."""
        if not self._store.is_enrolled():
            return False, "mfa_not_enrolled"
        if factors is None:
            return False, "mfa_required"
        level = classify_level(risk, tool_name)
        return self._store.verify(
            level=level,
            totp=factors.totp or "",
            humanity=factors.humanity,
            riddle_answer=factors.riddle_answer,
        )
