"""ToolPolicyStore.for_agent() / AgentToolPolicyView — Enterprise Fase 2 Phase 2.

Covers the per-agent policy_overlay precedence: RESTRICT-ONLY (sovereignty —
the cloud overlay may only narrow the local owner's policy, never widen it).
is_enabled is the INTERSECTION of the global store and the overlay:

  - No overlay entry for a tool -> falls through to the global store, unchanged.
  - An overlay entry disables a tool the global store enables -> disabled for
    THIS view only (the global store itself is untouched). Legitimate narrowing.
  - An overlay entry that tries to ENABLE a tool the global store disables ->
    stays DISABLED for this view. The overlay can NEVER re-enable past the
    owner's local disable (sovereignty fix: a cloud overlay must never widen
    past the local floor).
  - A malformed overlay entry (wrong shape/type) fails CLOSED: treated as an
    explicit disable, never silently falls through into a permissive default.
  - approval_on_dangers has no per-agent axis in this overlay shape: always defers
    to the global store.
"""

from __future__ import annotations

import pytest

from hermes.capabilities.tool_policy import Preset, ToolPolicyStore, resolve_approval_override

pytestmark = pytest.mark.unit

_AGENT_ID = "agent-a"


def _store(tmp_path) -> ToolPolicyStore:
    return ToolPolicyStore(path=tmp_path / "tool_policy.json")


class TestNoOverlayFallsThroughToGlobal:
    def test_empty_overlay_matches_global_is_enabled(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_tool("terminal", True)
        view = store.for_agent(_AGENT_ID, {})
        assert view.is_enabled("terminal") == store.is_enabled("terminal") is True

    def test_empty_overlay_matches_global_is_owner_disabled(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_tool("terminal", False)
        view = store.for_agent(_AGENT_ID, {})
        assert view.is_owner_disabled("terminal") == store.is_owner_disabled("terminal") is True

    def test_tool_absent_from_overlay_falls_through(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_tool("write_file", True)
        view = store.for_agent(_AGENT_ID, {"terminal": {"enabled": False}})
        # "write_file" has no overlay entry -> global behaviour applies.
        assert view.is_enabled("write_file") is True

    def test_approval_on_dangers_always_defers_to_global(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_approval_on_dangers(False)
        view = store.for_agent(_AGENT_ID, {"terminal": {"enabled": True}})
        assert view.approval_on_dangers() is False


class TestOverlayDisablesGloballyEnabledTool:
    def test_is_enabled_false_for_this_agent(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_tool("terminal", True)  # globally enabled
        view = store.for_agent(_AGENT_ID, {"terminal": {"enabled": False}})
        assert view.is_enabled("terminal") is False

    def test_is_owner_disabled_true_for_this_agent(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_tool("terminal", True)
        view = store.for_agent(_AGENT_ID, {"terminal": {"enabled": False}})
        assert view.is_owner_disabled("terminal") is True

    def test_global_store_itself_is_unaffected(self, tmp_path) -> None:
        """The overlay must never mutate the underlying global file."""
        store = _store(tmp_path)
        store.set_tool("terminal", True)
        store.for_agent(_AGENT_ID, {"terminal": {"enabled": False}})
        assert store.is_enabled("terminal") is True


class TestOverlayCannotEnableGloballyDisabledTool:
    """Sovereignty fix: the cloud overlay is RESTRICT-ONLY — it must NEVER
    re-enable a tool the local owner consciously disabled. Before the fix,
    an overlay {"enabled": True} on an owner-disabled tool widened the
    effective policy past the owner's floor; this class pins the fix."""

    def test_is_enabled_stays_false_for_this_agent(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_tool("delegate_task", False)  # owner disabled
        view = store.for_agent(_AGENT_ID, {"delegate_task": {"enabled": True}})
        assert view.is_enabled("delegate_task") is False

    def test_is_owner_disabled_stays_true_for_this_agent(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_tool("delegate_task", False)
        view = store.for_agent(_AGENT_ID, {"delegate_task": {"enabled": True}})
        assert view.is_owner_disabled("delegate_task") is True


class TestMalformedOverlayFailsClosed:
    def test_non_dict_entry_fails_closed_disabled(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_tool("terminal", True)
        view = store.for_agent(_AGENT_ID, {"terminal": "not-a-dict"})
        assert view.is_enabled("terminal") is False
        assert view.is_owner_disabled("terminal") is True

    def test_wrong_typed_enabled_value_fails_closed_disabled(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_tool("terminal", True)
        view = store.for_agent(_AGENT_ID, {"terminal": {"enabled": "yes"}})
        assert view.is_enabled("terminal") is False
        assert view.is_owner_disabled("terminal") is True

    def test_missing_enabled_key_fails_closed_disabled(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_tool("terminal", True)
        view = store.for_agent(_AGENT_ID, {"terminal": {}})
        assert view.is_enabled("terminal") is False

    def test_non_dict_overlay_object_falls_back_to_global(self, tmp_path) -> None:
        """A non-dict overlay AT ALL (defensive-only; the domain aggregate
        already guarantees policy_overlay is a dict) degrades to no-overlay,
        not to a blanket disable — matches ToolPolicyStore() direct behaviour."""
        store = _store(tmp_path)
        store.set_tool("terminal", True)
        view = store.for_agent(_AGENT_ID, overlay="not-a-dict")  # type: ignore[arg-type]
        assert view.is_enabled("terminal") is True


class TestPresetDefaultFallthrough:
    def test_overlay_absent_tool_uses_preset_default(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.apply_preset(Preset.PERMISIVO)
        view = store.for_agent(_AGENT_ID, {})
        assert view.is_enabled("delegate_task") is True

    def test_bloqueado_preset_flows_through_when_no_overlay(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.apply_preset(Preset.BLOQUEADO)
        view = store.for_agent(_AGENT_ID, {})
        assert view.is_owner_disabled("terminal") is True


# ---------------------------------------------------------------------------
# resolve_approval_override / AgentToolPolicyView.approval_override
# (item 2, ads-vertical) — the OPTIONAL "approval" axis, independent of
# "enabled".
# ---------------------------------------------------------------------------


class TestResolveApprovalOverridePureFunction:
    def test_absent_tool_returns_none(self) -> None:
        assert resolve_approval_override({"a": {"approval": "auto"}}, "b") is None

    def test_valid_auto_returned(self) -> None:
        overlay = {"mcp__safent-ads__propose_budget_change": {"approval": "auto"}}
        assert (
            resolve_approval_override(overlay, "mcp__safent-ads__propose_budget_change")
            == "auto"
        )

    def test_valid_hitl_returned(self) -> None:
        overlay = {"terminal": {"approval": "hitl"}}
        assert resolve_approval_override(overlay, "terminal") == "hitl"

    def test_invalid_value_returns_none(self) -> None:
        overlay = {"terminal": {"approval": "sometimes"}}
        assert resolve_approval_override(overlay, "terminal") is None

    def test_missing_approval_key_returns_none(self) -> None:
        overlay = {"terminal": {"enabled": True}}
        assert resolve_approval_override(overlay, "terminal") is None

    def test_non_dict_entry_returns_none(self) -> None:
        overlay = {"terminal": "not-a-dict"}
        assert resolve_approval_override(overlay, "terminal") is None

    def test_non_dict_overlay_returns_none(self) -> None:
        assert resolve_approval_override("not-a-dict", "terminal") is None  # type: ignore[arg-type]

    def test_enabled_and_approval_coexist(self) -> None:
        overlay = {"terminal": {"enabled": False, "approval": "auto"}}
        assert resolve_approval_override(overlay, "terminal") == "auto"


class TestAgentToolPolicyViewApprovalOverride:
    def test_delegates_to_module_function(self, tmp_path) -> None:
        store = _store(tmp_path)
        view = store.for_agent(_AGENT_ID, {"terminal": {"approval": "hitl"}})
        assert view.approval_override("terminal") == "hitl"

    def test_absent_returns_none(self, tmp_path) -> None:
        store = _store(tmp_path)
        view = store.for_agent(_AGENT_ID, {})
        assert view.approval_override("terminal") is None

    def test_enabled_only_entry_leaves_approval_none(self, tmp_path) -> None:
        """The two axes are independent: an entry that sets only "enabled"
        carries no approval override."""
        store = _store(tmp_path)
        view = store.for_agent(_AGENT_ID, {"terminal": {"enabled": False}})
        assert view.approval_override("terminal") is None
