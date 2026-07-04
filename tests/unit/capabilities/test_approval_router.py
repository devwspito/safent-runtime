"""Tests for approval_router — Enterprise approval routing (Fase 2 Phase 4a).

Full truth table over the four boolean-ish inputs that decide ENTERPRISE vs
LOCAL: tenant gate (agent_managed_by == "cloud" AND
tenant_remote_approval_enabled) x eligibility (delicacy is MOST_DELICATE OR
sensitivity_categories non-empty OR irreversible). HARDBLOCK is never
produced by route() under any input combination.
"""

from __future__ import annotations

import itertools

import pytest

from hermes.capabilities.approval_router import ApprovalRoute, route
from hermes.capabilities.tool_delicacy import Delicacy
from hermes.capabilities.tool_sensitivity import SensitivityCategory

pytestmark = pytest.mark.unit

_SOME_SENSITIVITY = frozenset({SensitivityCategory.PII_READ})


def _route(
    *,
    delicacy: Delicacy = Delicacy.NORMAL,
    sensitivity_categories: frozenset[SensitivityCategory] = frozenset(),
    irreversible: bool = False,
    agent_managed_by: str | None = None,
    tenant_remote_approval_enabled: bool = False,
) -> ApprovalRoute:
    return route(
        tool="some_tool",
        delicacy=delicacy,
        sensitivity_categories=sensitivity_categories,
        irreversible=irreversible,
        agent_managed_by=agent_managed_by,
        tenant_remote_approval_enabled=tenant_remote_approval_enabled,
    )


# ---------------------------------------------------------------------------
# Tenant gate — ENTERPRISE requires BOTH cloud-managed AND the tenant flag
# ---------------------------------------------------------------------------


class TestTenantGating:
    def test_cloud_managed_and_enabled_and_eligible_is_enterprise(self) -> None:
        result = _route(
            delicacy=Delicacy.MOST_DELICATE,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
        )
        assert result is ApprovalRoute.ENTERPRISE

    def test_cloud_managed_but_flag_disabled_is_local(self) -> None:
        result = _route(
            delicacy=Delicacy.MOST_DELICATE,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=False,
        )
        assert result is ApprovalRoute.LOCAL

    def test_flag_enabled_but_not_cloud_managed_is_local(self) -> None:
        result = _route(
            delicacy=Delicacy.MOST_DELICATE,
            agent_managed_by=None,
            tenant_remote_approval_enabled=True,
        )
        assert result is ApprovalRoute.LOCAL

    def test_managed_by_other_than_cloud_string_is_local(self) -> None:
        result = _route(
            delicacy=Delicacy.MOST_DELICATE,
            agent_managed_by="local-owner",
            tenant_remote_approval_enabled=True,
        )
        assert result is ApprovalRoute.LOCAL

    def test_neither_cloud_managed_nor_enabled_is_local(self) -> None:
        result = _route(delicacy=Delicacy.MOST_DELICATE)
        assert result is ApprovalRoute.LOCAL


# ---------------------------------------------------------------------------
# Eligibility — even a fully cloud-gated tenant stays LOCAL for an ordinary
# (non-eligible) action
# ---------------------------------------------------------------------------


class TestEligibilityGating:
    def test_cloud_gated_but_not_eligible_is_local(self) -> None:
        result = _route(
            delicacy=Delicacy.NORMAL,
            sensitivity_categories=frozenset(),
            irreversible=False,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
        )
        assert result is ApprovalRoute.LOCAL

    def test_cloud_gated_and_delicate_but_not_most_delicate_is_local(self) -> None:
        result = _route(
            delicacy=Delicacy.DELICATE,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
        )
        assert result is ApprovalRoute.LOCAL

    def test_cloud_gated_and_most_delicate_is_enterprise(self) -> None:
        result = _route(
            delicacy=Delicacy.MOST_DELICATE,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
        )
        assert result is ApprovalRoute.ENTERPRISE

    def test_cloud_gated_and_sensitive_is_enterprise(self) -> None:
        result = _route(
            sensitivity_categories=_SOME_SENSITIVITY,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
        )
        assert result is ApprovalRoute.ENTERPRISE

    def test_cloud_gated_and_irreversible_is_enterprise(self) -> None:
        result = _route(
            irreversible=True,
            agent_managed_by="cloud",
            tenant_remote_approval_enabled=True,
        )
        assert result is ApprovalRoute.ENTERPRISE


# ---------------------------------------------------------------------------
# HARDBLOCK is never produced
# ---------------------------------------------------------------------------


class TestHardblockNeverProduced:
    def test_hardblock_never_emitted_across_full_truth_table(self) -> None:
        delicacies = list(Delicacy)
        sensitivities = (frozenset(), _SOME_SENSITIVITY)
        booleans = (False, True)
        managed_by_values = (None, "cloud", "local-owner")

        for d, s, irr, mgd, enabled in itertools.product(
            delicacies, sensitivities, booleans, managed_by_values, booleans
        ):
            result = _route(
                delicacy=d,
                sensitivity_categories=s,
                irreversible=irr,
                agent_managed_by=mgd,
                tenant_remote_approval_enabled=enabled,
            )
            assert result is not ApprovalRoute.HARDBLOCK
            assert result in (ApprovalRoute.LOCAL, ApprovalRoute.ENTERPRISE)


# ---------------------------------------------------------------------------
# Full truth table (explicit, parametrized) — belt-and-suspenders on top of
# the targeted tests above.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent_managed_by", "tenant_remote_approval_enabled", "eligible", "expected"),
    [
        (None, False, False, ApprovalRoute.LOCAL),
        (None, False, True, ApprovalRoute.LOCAL),
        (None, True, False, ApprovalRoute.LOCAL),
        (None, True, True, ApprovalRoute.LOCAL),
        ("cloud", False, False, ApprovalRoute.LOCAL),
        ("cloud", False, True, ApprovalRoute.LOCAL),
        ("cloud", True, False, ApprovalRoute.LOCAL),
        ("cloud", True, True, ApprovalRoute.ENTERPRISE),
    ],
)
def test_full_truth_table(
    agent_managed_by: str | None,
    tenant_remote_approval_enabled: bool,
    eligible: bool,
    expected: ApprovalRoute,
) -> None:
    result = _route(
        delicacy=Delicacy.MOST_DELICATE if eligible else Delicacy.NORMAL,
        agent_managed_by=agent_managed_by,
        tenant_remote_approval_enabled=tenant_remote_approval_enabled,
    )
    assert result is expected
