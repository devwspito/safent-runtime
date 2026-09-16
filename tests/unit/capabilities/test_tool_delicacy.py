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

import pytest

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
        """is_mfa_required is a SEPARATE hand-curated axis (_ENTERPRISE_REVIEW_TOOLS) —
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


# ---------------------------------------------------------------------------
# safent-ads companion (024/T091/T194) — MFA tier
#
# Ninguna tool del companion es MFA-tier: propose_*/withdraw_proposal crean
# una fila pendiente que aprueba un humano por otro canal, y
# apply_defensive_action solo admite lower_budget|pause tras el chokepoint del
# companion. El eje `approval` del overlay decide (ver tool_delicacy.py y
# test_broker_approval_override.py::TestT194AdsApplyDefensiveActionHonoursApprovalAxis).
# ---------------------------------------------------------------------------


class TestAdsCompanionMfaTier:
    def test_apply_defensive_action_is_not_mfa_tier(self) -> None:
        # Solo admite lower_budget|pause y pasa por el chokepoint del companion:
        # el eje `approval` del overlay decide (pausar/bajar autonomo), nunca MFA.
        assert is_mfa_required("mcp__safent-ads__apply_defensive_action") is False

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__safent-ads__propose_budget_change",
            "mcp__safent-ads__propose_pause",
            "mcp__safent-ads__propose_targeting_change",
            "mcp__safent-ads__propose_creative_publication",
            "mcp__safent-ads__withdraw_proposal",
        ],
    )
    def test_propose_and_withdraw_tools_are_not_mfa_tier(self, tool_name: str) -> None:
        """These never touch the real platform — the SPEND classification
        (tool_sensitivity) still applies for audit context, but the
        override-widening mechanism must stay open for them, exactly like
        today (test_broker_approval_override.py's existing coverage)."""
        assert is_mfa_required(tool_name) is False

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__safent-ads__list_campaigns",
            "mcp__safent-ads__get_campaign",
            "mcp__safent-ads__get_kill_switch_status",
        ],
    )
    def test_ads_read_tools_are_not_mfa_tier(self, tool_name: str) -> None:
        assert is_mfa_required(tool_name) is False

    def test_matching_is_by_full_qualified_name_not_bare_tool_name(self) -> None:
        """Same anti-relabeling contract as tool_sensitivity's SPEND set —
        the bare tool name, or a different companion's slug, never
        inherits the MFA tier."""
        assert is_mfa_required("apply_defensive_action") is False
        assert is_mfa_required("mcp__some-other-companion__apply_defensive_action") is False
