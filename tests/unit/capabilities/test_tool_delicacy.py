"""tool_delicacy — single source of truth for owner-approval tiers.

LOW fix (delegate_to_colleague tier): `delegate_to_colleague` is a cage-
escaping outbound comms action (reaches another human's assistant), the SAME
class as send_message — but it is NOT a native Nous tool, so
classify_nous_tool() can never derive DELICATE for it automatically. Confirms
delicacy() now aligns it with send_message's tier, and that the REAL HITL
gate (ExtendedCapabilityBinding.auto_executable=False) still fires
independently of this classification.
"""

from __future__ import annotations

from hermes.capabilities.application.capability_registry import CapabilityRegistry
from hermes.capabilities.tool_delicacy import (
    Delicacy,
    default_enabled_equilibrado,
    delicacy,
    is_mfa_required,
)


class TestDelegateToColleagueDelicacyTier:
    def test_delegate_to_colleague_is_delicate_like_send_message(self) -> None:
        assert delicacy("delegate_to_colleague") == Delicacy.DELICATE
        assert delicacy("delegate_to_colleague") == delicacy("send_message")

    def test_delegate_to_colleague_is_not_most_delicate(self) -> None:
        """Unlike install_*/set_policy/etc, this doesn't widen the agent's own
        capabilities — DELICATE (not MOST_DELICATE) is the correct tier."""
        assert delicacy("delegate_to_colleague") != Delicacy.MOST_DELICATE

    def test_delegate_to_colleague_stays_default_enabled(self) -> None:
        """DELICATE tools are still ON by default in Equilibrado — only
        MOST_DELICATE requires explicit owner opt-in."""
        assert default_enabled_equilibrado("delegate_to_colleague") is True

    def test_delegate_to_colleague_is_not_forced_into_mfa_hitl_tier(self) -> None:
        """is_mfa_required is a SEPARATE hand-curated axis (_MFA_TIER_HITL) —
        changing delicacy() must not accidentally force TOTP on this tool."""
        assert is_mfa_required("delegate_to_colleague") is False


class TestDelegateToColleagueCapabilityBindingStillForcesHitl:
    def test_capability_binding_forces_write_proposal_not_auto_executable(self) -> None:
        """The PRIMARY HITL gate: regardless of the delicacy tier above, the
        capability binding must keep auto_executable=False (owner Approve/
        Reject is mandatory for every delegate_to_colleague call)."""
        binding = CapabilityRegistry().resolve("delegate_to_colleague")
        assert binding is not None
        assert binding.auto_executable is False

    def test_capability_binding_is_high_risk_and_never_persistent(self) -> None:
        from hermes.capabilities.domain.ports import RiskLevel

        binding = CapabilityRegistry().resolve("delegate_to_colleague")
        assert binding is not None
        assert binding.risk is RiskLevel.HIGH
        assert binding.persistent_forbidden is True
