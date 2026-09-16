"""Governance classification for tailnet_ssh/tailnet_file_get/tailnet_file_put
(spec 022 v2): delicacy, sensitivity, policy catalog, and the invariant that
none of this can be turned into a hard-block-free "auto" tool — the REAL
per-host HITL gate lives in `security_hook._resolve_tailnet_ssh_consent`,
independent of everything tested here (see that module's own tests)."""

from __future__ import annotations

import pytest

from hermes.capabilities.tool_delicacy import (
    Delicacy,
    default_enabled_equilibrado,
    delicacy,
    is_mfa_required,
)
from hermes.capabilities.tool_policy import TOOL_CATALOG, _tool_category, _tool_origin
from hermes.capabilities.tool_sensitivity import SensitivityCategory, sensitivity
from hermes.tailnet_ssh.tool_names import TAILNET_SSH_TOOL_NAMES

pytestmark = pytest.mark.unit


class TestTailnetSshDelicacy:
    @pytest.mark.parametrize("tool", sorted(TAILNET_SSH_TOOL_NAMES))
    def test_is_delicate(self, tool: str) -> None:
        assert delicacy(tool) is Delicacy.DELICATE

    @pytest.mark.parametrize("tool", sorted(TAILNET_SSH_TOOL_NAMES))
    def test_is_not_most_delicate(self, tool: str) -> None:
        """MOST_DELICATE would additionally require a riddle MFA on the
        generic gate — tailnet_ssh's real gate is the bespoke per-host card,
        not the generic escalation ladder."""
        assert delicacy(tool) is not Delicacy.MOST_DELICATE

    @pytest.mark.parametrize("tool", sorted(TAILNET_SSH_TOOL_NAMES))
    def test_default_enabled_in_equilibrado(self, tool: str) -> None:
        """DELICATE tools stay ON by default — the per-host card, not the
        Equilibrado toggle, is what actually gates each call."""
        assert default_enabled_equilibrado(tool) is True

    @pytest.mark.parametrize("tool", sorted(TAILNET_SSH_TOOL_NAMES))
    def test_is_not_forced_into_totp_mfa_tier(self, tool: str) -> None:
        """is_mfa_required is the SEPARATE TOTP-at-approval axis
        (_ENTERPRISE_REVIEW_TOOLS) — tailnet_ssh intentionally is NOT on it (simple
        Aprobar/Rechazar card, like send_message/ha_call_service)."""
        assert is_mfa_required(tool) is False


class TestTailnetSshSensitivity:
    @pytest.mark.parametrize("tool", sorted(TAILNET_SSH_TOOL_NAMES))
    def test_classified_as_remote_exec(self, tool: str) -> None:
        assert SensitivityCategory.REMOTE_EXEC in sensitivity(tool, {"host": "db1"})

    def test_unrelated_tool_not_classified_as_remote_exec(self) -> None:
        categories = sensitivity("read_file", {"path": "/etc/hostname"})
        assert SensitivityCategory.REMOTE_EXEC not in categories

    def test_classification_is_fail_soft_on_bad_args(self) -> None:
        """REMOTE_EXEC keys off tool_name alone — a malformed `args` must
        never crash `sensitivity()` (the fail-soft contract every other
        category in this module also honours)."""
        assert sensitivity("tailnet_ssh", None) == frozenset({  # type: ignore[arg-type]
            SensitivityCategory.REMOTE_EXEC
        })


class TestTailnetSshPolicyCatalog:
    @pytest.mark.parametrize("tool", sorted(TAILNET_SSH_TOOL_NAMES))
    def test_listed_in_catalog(self, tool: str) -> None:
        """Present in the Policies UI — the owner can hard-disable it
        (Step 1.5 of the pre-tool-call hook), independent of the per-host
        HITL gate."""
        assert tool in TOOL_CATALOG

    @pytest.mark.parametrize("tool", sorted(TAILNET_SSH_TOOL_NAMES))
    def test_category_is_tailnet_ssh(self, tool: str) -> None:
        assert _tool_category(tool, origin="capability") == "Tailnet / SSH"

    @pytest.mark.parametrize("tool", sorted(TAILNET_SSH_TOOL_NAMES))
    def test_origin_is_capability_not_native(self, tool: str) -> None:
        """Not a native Nous tool (never dispatched through nous_engine) —
        must not be mis-reported as "native" in the Policies UI."""
        assert _tool_origin(tool) == "capability"


class TestTailnetSshToolNamesIsTheOnlySourceOfTruth:
    def test_exactly_three_tools(self) -> None:
        assert {
            "tailnet_ssh", "tailnet_file_get", "tailnet_file_put",
        } == TAILNET_SSH_TOOL_NAMES
