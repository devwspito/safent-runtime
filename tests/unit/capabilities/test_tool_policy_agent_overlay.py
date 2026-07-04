"""ToolPolicyStore.for_agent() / AgentToolPolicyView — Enterprise Fase 2 Phase 2.

Covers the per-agent policy_overlay precedence (agent overlay -> global file ->
preset default):

  - No overlay entry for a tool -> falls through to the global store, unchanged.
  - An overlay entry disables a tool the global store enables -> disabled for
    THIS view only (the global store itself is untouched).
  - An overlay entry enables a tool the global store disables -> enabled for
    THIS view (full precedence, either direction, per the pinned contract).
  - A malformed overlay entry (wrong shape/type) fails CLOSED: treated as an
    explicit disable, never silently falls through into a permissive default.
  - mfa_on_dangers has no per-agent axis in this overlay shape: always defers
    to the global store.
"""

from __future__ import annotations

import pytest

from hermes.capabilities.tool_policy import Preset, ToolPolicyStore

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

    def test_mfa_on_dangers_always_defers_to_global(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_mfa_on_dangers(False)
        view = store.for_agent(_AGENT_ID, {"terminal": {"enabled": True}})
        assert view.mfa_on_dangers() is False


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


class TestOverlayEnablesGloballyDisabledTool:
    def test_is_enabled_true_for_this_agent(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_tool("delegate_task", False)  # globally disabled
        view = store.for_agent(_AGENT_ID, {"delegate_task": {"enabled": True}})
        assert view.is_enabled("delegate_task") is True

    def test_is_owner_disabled_false_for_this_agent(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.set_tool("delegate_task", False)
        view = store.for_agent(_AGENT_ID, {"delegate_task": {"enabled": True}})
        assert view.is_owner_disabled("delegate_task") is False


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
