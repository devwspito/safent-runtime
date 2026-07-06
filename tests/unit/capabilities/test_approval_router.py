"""Tests for approval_router — TOTP-keyed Enterprise approval routing
(Fase 2 Phase 4c).

route() is keyed purely on the tenant gate AND `tool_delicacy.
is_mfa_required(tool)` — the worker has no TOTP (centralized at Enterprise),
so an MFA-tier action on a cloud-managed, remote-approval-enabled tenant MUST
route ENTERPRISE; every other combination stays LOCAL. HARDBLOCK is never
produced by route() under any input combination. `approval_tier` no longer
affects routing (inert, kept for back-compat/observability).
"""

from __future__ import annotations

import itertools

import pytest

from hermes.capabilities.approval_router import ApprovalRoute, route
from hermes.capabilities.tool_delicacy import _MFA_TIER_HITL, is_mfa_required

pytestmark = pytest.mark.unit

# send_message: native WRITE (DELICATE), NOT MFA-tier — the owner's
# report-review example: "my boss asks my agent for a report — I still
# review it before it sends", plain click, no TOTP, no Enterprise.
_SIMPLE_TOOL = "send_message"
# skill_manage: MOST_DELICATE AND MFA-tier — self-widening action, TOTP-gated.
_MFA_TOOL = "skill_manage"
# cronjob: MOST_DELICATE by delicacy() (blocks unconditionally at the hook)
# but explicitly carved out of the MFA tier (owner decision 2026-06-25) — a
# plain click suffices. Distinguishes the TWO axes: delicacy() (hook_mfa_block)
# vs is_mfa_required() (routing/approve()). Must NEVER escalate to ENTERPRISE.
_MOST_DELICATE_BUT_SIMPLE_TOOL = "cronjob"


def _route(
    tool: str,
    *,
    agent_managed_by: str | None = None,
    tenant_remote_approval_enabled: bool = False,
) -> ApprovalRoute:
    return route(
        tool=tool,
        agent_managed_by=agent_managed_by,
        tenant_remote_approval_enabled=tenant_remote_approval_enabled,
    )


# ---------------------------------------------------------------------------
# Sanity: the fixture tools are classified as expected by tool_delicacy
# ---------------------------------------------------------------------------


def test_fixture_tools_carry_the_expected_mfa_tier() -> None:
    assert is_mfa_required(_SIMPLE_TOOL) is False
    assert is_mfa_required(_MFA_TOOL) is True
    assert is_mfa_required(_MOST_DELICATE_BUT_SIMPLE_TOOL) is False


# ---------------------------------------------------------------------------
# Tenant gate — ENTERPRISE requires BOTH cloud-managed AND the tenant flag
# ---------------------------------------------------------------------------


class TestTenantGating:
    def test_cloud_managed_and_enabled_and_mfa_tier_is_enterprise(self) -> None:
        result = _route(
            _MFA_TOOL, agent_managed_by="cloud", tenant_remote_approval_enabled=True
        )
        assert result is ApprovalRoute.ENTERPRISE

    def test_cloud_managed_but_flag_disabled_is_local(self) -> None:
        result = _route(
            _MFA_TOOL, agent_managed_by="cloud", tenant_remote_approval_enabled=False
        )
        assert result is ApprovalRoute.LOCAL

    def test_flag_enabled_but_not_cloud_managed_is_local(self) -> None:
        result = _route(
            _MFA_TOOL, agent_managed_by=None, tenant_remote_approval_enabled=True
        )
        assert result is ApprovalRoute.LOCAL

    def test_managed_by_other_than_cloud_string_is_local(self) -> None:
        result = _route(
            _MFA_TOOL, agent_managed_by="local-owner", tenant_remote_approval_enabled=True
        )
        assert result is ApprovalRoute.LOCAL

    def test_neither_cloud_managed_nor_enabled_is_local(self) -> None:
        assert _route(_MFA_TOOL) is ApprovalRoute.LOCAL


# ---------------------------------------------------------------------------
# MFA-tier gating — even a fully cloud-gated tenant stays LOCAL for a
# non-MFA-tier (simple) action; the worker approves it alone.
# ---------------------------------------------------------------------------


class TestMfaTierGating:
    def test_cloud_gated_but_simple_tool_is_local(self) -> None:
        result = _route(
            _SIMPLE_TOOL, agent_managed_by="cloud", tenant_remote_approval_enabled=True
        )
        assert result is ApprovalRoute.LOCAL

    def test_cloud_gated_and_mfa_tier_is_enterprise(self) -> None:
        result = _route(
            _MFA_TOOL, agent_managed_by="cloud", tenant_remote_approval_enabled=True
        )
        assert result is ApprovalRoute.ENTERPRISE

    def test_most_delicate_by_delicacy_but_simple_by_mfa_tier_stays_local(self) -> None:
        """cronjob is MOST_DELICATE (blocks at the hook unconditionally) but
        explicitly carved OUT of the MFA tier — routing must follow
        is_mfa_required, NOT the coarser delicacy() axis, even fully gated."""
        result = _route(
            _MOST_DELICATE_BUT_SIMPLE_TOOL,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
        )
        assert result is ApprovalRoute.LOCAL

    def test_unknown_tool_is_never_mfa_tier_and_stays_local(self) -> None:
        result = _route(
            "some_unclassified_tool",
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
        )
        assert result is ApprovalRoute.LOCAL


# ---------------------------------------------------------------------------
# approval_tier is INERT — a coordinator and a standard agent route identically
# ---------------------------------------------------------------------------


class TestApprovalTierIsInert:
    def test_coordinator_and_standard_route_identically_for_mfa_tool(self) -> None:
        coordinator = route(
            tool=_MFA_TOOL,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
            approval_tier="coordinator",
        )
        standard = route(
            tool=_MFA_TOOL,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
            approval_tier="standard",
        )
        assert coordinator is standard is ApprovalRoute.ENTERPRISE

    def test_coordinator_and_standard_route_identically_for_simple_tool(self) -> None:
        coordinator = route(
            tool=_SIMPLE_TOOL,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
            approval_tier="coordinator",
        )
        standard = route(
            tool=_SIMPLE_TOOL,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
            approval_tier="standard",
        )
        assert coordinator is standard is ApprovalRoute.LOCAL

    def test_unknown_tier_does_not_change_routing(self) -> None:
        result = route(
            tool=_MFA_TOOL,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
            approval_tier="weird",
        )
        assert result is ApprovalRoute.ENTERPRISE


# ---------------------------------------------------------------------------
# HARDBLOCK is never produced
# ---------------------------------------------------------------------------


class TestHardblockNeverProduced:
    def test_hardblock_never_emitted_across_full_truth_table(self) -> None:
        tools = (_SIMPLE_TOOL, _MFA_TOOL, _MOST_DELICATE_BUT_SIMPLE_TOOL, "unclassified")
        managed_by_values = (None, "cloud", "local-owner")
        booleans = (False, True)

        for tool, mgd, enabled in itertools.product(tools, managed_by_values, booleans):
            result = _route(tool, agent_managed_by=mgd, tenant_remote_approval_enabled=enabled)
            assert result is not ApprovalRoute.HARDBLOCK
            assert result in (ApprovalRoute.LOCAL, ApprovalRoute.ENTERPRISE)


# ---------------------------------------------------------------------------
# Full truth table (explicit, parametrized) — belt-and-suspenders on top of
# the targeted tests above. The routing set must equal the is_mfa_required
# set EXACTLY under a full tenant gate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent_managed_by", "tenant_remote_approval_enabled", "tool", "expected"),
    [
        (None, False, _SIMPLE_TOOL, ApprovalRoute.LOCAL),
        (None, False, _MFA_TOOL, ApprovalRoute.LOCAL),
        (None, True, _SIMPLE_TOOL, ApprovalRoute.LOCAL),
        (None, True, _MFA_TOOL, ApprovalRoute.LOCAL),
        ("cloud", False, _SIMPLE_TOOL, ApprovalRoute.LOCAL),
        ("cloud", False, _MFA_TOOL, ApprovalRoute.LOCAL),
        ("cloud", True, _SIMPLE_TOOL, ApprovalRoute.LOCAL),
        ("cloud", True, _MFA_TOOL, ApprovalRoute.ENTERPRISE),
    ],
)
def test_full_truth_table(
    agent_managed_by: str | None,
    tenant_remote_approval_enabled: bool,
    tool: str,
    expected: ApprovalRoute,
) -> None:
    result = _route(
        tool, agent_managed_by=agent_managed_by,
        tenant_remote_approval_enabled=tenant_remote_approval_enabled,
    )
    assert result is expected


def test_routing_set_equals_is_mfa_required_set_under_full_tenant_gate() -> None:
    """Assert the routing set == the is_mfa_required set exactly, per the
    corrected model's invariant ("carries TOTP <=> Enterprise")."""
    sample_tools = (
        "send_message", "write_file", "read_file", "delegate_to_colleague",
        *sorted(_MFA_TIER_HITL), "cronjob",
    )
    for tool in sample_tools:
        result = _route(tool, agent_managed_by="cloud", tenant_remote_approval_enabled=True)
        expected = ApprovalRoute.ENTERPRISE if is_mfa_required(tool) else ApprovalRoute.LOCAL
        assert result is expected, f"tool={tool!r} routing diverged from is_mfa_required"
