"""Tests for the enriched ToolPolicyStore.snapshot() catalog field.

Covers:
  - catalog field is present and non-empty on a fresh store.
  - Every entry has the required keys with correct types.
  - Static TOOL_CATALOG tools all appear in the catalog.
  - Dynamic tools published to DynamicToolRegistry appear in the catalog.
  - Parity test: every tool the LLM would receive (via _tools_source) appears
    in the catalog — fails if a dynamic tool is invisible to the Policies UI.
  - Backwards-compat: the flat tools:{name:bool} map is still present.
  - llm_visible correctly marks suppressed tools as False.
  - is_enabled behaviour is unchanged after snapshot() enrichment.
"""

from __future__ import annotations

import pytest

from hermes.capabilities.dynamic_tool_registry import (
    DynamicToolEntry,
    DynamicToolRegistry,
    get_dynamic_tool_registry,
)
from hermes.capabilities.tool_policy import TOOL_CATALOG, ToolPolicyStore

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_ENTRY_KEYS = {"name", "label", "category", "delicacy", "enabled",
                         "llm_visible", "origin"}
_VALID_DELICACY = {"normal", "delicate", "most_delicate"}
_VALID_ORIGINS = {"native", "capability", "mcp", "composio"}


def _fresh_store(tmp_path) -> ToolPolicyStore:
    return ToolPolicyStore(path=tmp_path / "tool_policy.json")


def _reset_dynamic_registry() -> None:
    """Clear the process-scoped singleton between tests."""
    get_dynamic_tool_registry().publish(())


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------


class TestCatalogStructure:
    def test_catalog_field_present(self, tmp_path) -> None:
        _reset_dynamic_registry()
        snap = _fresh_store(tmp_path).snapshot()
        assert "catalog" in snap

    def test_catalog_is_list(self, tmp_path) -> None:
        _reset_dynamic_registry()
        snap = _fresh_store(tmp_path).snapshot()
        assert isinstance(snap["catalog"], list)

    def test_catalog_non_empty_with_static_tools(self, tmp_path) -> None:
        _reset_dynamic_registry()
        snap = _fresh_store(tmp_path).snapshot()
        assert len(snap["catalog"]) >= len(TOOL_CATALOG)

    def test_each_entry_has_required_keys(self, tmp_path) -> None:
        _reset_dynamic_registry()
        snap = _fresh_store(tmp_path).snapshot()
        for entry in snap["catalog"]:
            missing = _REQUIRED_ENTRY_KEYS - entry.keys()
            assert not missing, f"Entry {entry['name']!r} missing keys: {missing}"

    def test_delicacy_values_valid(self, tmp_path) -> None:
        _reset_dynamic_registry()
        snap = _fresh_store(tmp_path).snapshot()
        for entry in snap["catalog"]:
            assert entry["delicacy"] in _VALID_DELICACY, (
                f"Entry {entry['name']!r} has invalid delicacy: {entry['delicacy']!r}"
            )

    def test_origin_values_valid(self, tmp_path) -> None:
        _reset_dynamic_registry()
        snap = _fresh_store(tmp_path).snapshot()
        for entry in snap["catalog"]:
            assert entry["origin"] in _VALID_ORIGINS, (
                f"Entry {entry['name']!r} has invalid origin: {entry['origin']!r}"
            )

    def test_enabled_is_bool(self, tmp_path) -> None:
        _reset_dynamic_registry()
        snap = _fresh_store(tmp_path).snapshot()
        for entry in snap["catalog"]:
            assert isinstance(entry["enabled"], bool), (
                f"Entry {entry['name']!r} enabled is not bool: {entry['enabled']!r}"
            )

    def test_llm_visible_is_bool(self, tmp_path) -> None:
        _reset_dynamic_registry()
        snap = _fresh_store(tmp_path).snapshot()
        for entry in snap["catalog"]:
            assert isinstance(entry["llm_visible"], bool), (
                f"Entry {entry['name']!r} llm_visible is not bool"
            )


# ---------------------------------------------------------------------------
# Static catalog coverage
# ---------------------------------------------------------------------------


class TestStaticCatalogCoverage:
    def test_all_static_catalog_tools_in_catalog(self, tmp_path) -> None:
        """Every tool in TOOL_CATALOG must appear in catalog."""
        _reset_dynamic_registry()
        snap = _fresh_store(tmp_path).snapshot()
        catalog_names = {e["name"] for e in snap["catalog"]}
        missing = TOOL_CATALOG - catalog_names
        assert not missing, (
            f"Static TOOL_CATALOG tools missing from catalog: {sorted(missing)}"
        )

    def test_backwards_compat_tools_map_present(self, tmp_path) -> None:
        """The flat tools:{name:bool} map must still be in the snapshot."""
        _reset_dynamic_registry()
        snap = _fresh_store(tmp_path).snapshot()
        assert "tools" in snap
        assert isinstance(snap["tools"], dict)
        assert set(snap["tools"]) == TOOL_CATALOG

    def test_preset_present(self, tmp_path) -> None:
        _reset_dynamic_registry()
        snap = _fresh_store(tmp_path).snapshot()
        assert snap["preset"] == "equilibrado"


# ---------------------------------------------------------------------------
# Dynamic tool parity — THE CORE REGRESSION TEST
#
# This test fails if a tool that the LLM receives (published via _tools_source
# into DynamicToolRegistry) is absent from the snapshot catalog.
# ---------------------------------------------------------------------------


class TestDynamicToolParity:
    def test_mcp_tool_appears_in_catalog_after_publish(self, tmp_path) -> None:
        """Publishing an MCP tool makes it visible in the Policies catalog."""
        registry = get_dynamic_tool_registry()
        registry.publish((
            DynamicToolEntry(name="mcp__ruflo__web_search", origin="mcp"),
            DynamicToolEntry(name="mcp__ruflo__read_file", origin="mcp"),
        ))
        snap = _fresh_store(tmp_path).snapshot()
        catalog_names = {e["name"] for e in snap["catalog"]}
        assert "mcp__ruflo__web_search" in catalog_names
        assert "mcp__ruflo__read_file" in catalog_names
        _reset_dynamic_registry()

    def test_composio_tool_appears_in_catalog_after_publish(self, tmp_path) -> None:
        """Publishing a Composio tool makes it visible in the Policies catalog."""
        registry = get_dynamic_tool_registry()
        registry.publish((
            DynamicToolEntry(name="gmail_send_email", origin="composio"),
        ))
        snap = _fresh_store(tmp_path).snapshot()
        catalog_names = {e["name"] for e in snap["catalog"]}
        assert "gmail_send_email" in catalog_names
        _reset_dynamic_registry()

    def test_dynamic_tool_has_correct_origin(self, tmp_path) -> None:
        registry = get_dynamic_tool_registry()
        registry.publish((
            DynamicToolEntry(name="mcp__test__query", origin="mcp"),
            DynamicToolEntry(name="googledrive_list_files", origin="composio"),
        ))
        snap = _fresh_store(tmp_path).snapshot()
        by_name = {e["name"]: e for e in snap["catalog"]}
        assert by_name["mcp__test__query"]["origin"] == "mcp"
        assert by_name["googledrive_list_files"]["origin"] == "composio"
        _reset_dynamic_registry()

    def test_dynamic_tool_has_correct_category(self, tmp_path) -> None:
        registry = get_dynamic_tool_registry()
        registry.publish((
            DynamicToolEntry(name="mcp__ruflo__search", origin="mcp"),
            DynamicToolEntry(name="slack_send_dm", origin="composio"),
        ))
        snap = _fresh_store(tmp_path).snapshot()
        by_name = {e["name"]: e for e in snap["catalog"]}
        assert by_name["mcp__ruflo__search"]["category"] == "Herramientas externas (MCP)"
        assert by_name["slack_send_dm"]["category"] == "Apps conectadas (Composio)"
        _reset_dynamic_registry()

    def test_dynamic_tool_enabled_by_default(self, tmp_path) -> None:
        """Dynamic tools without an override follow the preset default (Equilibrado → ON)."""
        registry = get_dynamic_tool_registry()
        registry.publish((
            DynamicToolEntry(name="mcp__ruflo__list_resources", origin="mcp"),
        ))
        snap = _fresh_store(tmp_path).snapshot()
        by_name = {e["name"]: e for e in snap["catalog"]}
        assert by_name["mcp__ruflo__list_resources"]["enabled"] is True
        _reset_dynamic_registry()

    def test_dynamic_tool_llm_visible(self, tmp_path) -> None:
        """Dynamic tools published to the registry are llm_visible=True."""
        registry = get_dynamic_tool_registry()
        registry.publish((
            DynamicToolEntry(name="mcp__ruflo__fetch_url", origin="mcp"),
        ))
        snap = _fresh_store(tmp_path).snapshot()
        by_name = {e["name"]: e for e in snap["catalog"]}
        assert by_name["mcp__ruflo__fetch_url"]["llm_visible"] is True
        _reset_dynamic_registry()

    def test_clearing_dynamic_registry_removes_dynamic_from_catalog(
        self, tmp_path
    ) -> None:
        """Clearing the registry removes dynamic-only tools from the next snapshot."""
        registry = get_dynamic_tool_registry()
        registry.publish((
            DynamicToolEntry(name="mcp__only_in_dynamic__tool", origin="mcp"),
        ))
        # Confirm it's there.
        snap_before = _fresh_store(tmp_path).snapshot()
        assert any(
            e["name"] == "mcp__only_in_dynamic__tool"
            for e in snap_before["catalog"]
        )
        # Clear and re-snapshot.
        _reset_dynamic_registry()
        snap_after = _fresh_store(tmp_path).snapshot()
        assert not any(
            e["name"] == "mcp__only_in_dynamic__tool"
            for e in snap_after["catalog"]
        )

    def test_parity_all_published_tools_in_catalog(self, tmp_path) -> None:
        """THE parity gate: every tool the LLM receives must appear in the catalog.

        Simulates what _tools_source publishes after a cycle with active MCP + Composio
        tools.  If any tool is absent from the catalog the Policies UI is blind to it.
        """
        simulated_llm_tools = [
            DynamicToolEntry(name="mcp__ruflo__web_search", origin="mcp"),
            DynamicToolEntry(name="mcp__ruflo__browse", origin="mcp"),
            DynamicToolEntry(name="gmail_send_email", origin="composio"),
            DynamicToolEntry(name="github_list_repos", origin="composio"),
            DynamicToolEntry(name="slack_post_message", origin="composio"),
        ]
        registry = get_dynamic_tool_registry()
        registry.publish(tuple(simulated_llm_tools))

        snap = _fresh_store(tmp_path).snapshot()
        catalog_names = {e["name"] for e in snap["catalog"]}

        invisible = [
            t.name for t in simulated_llm_tools if t.name not in catalog_names
        ]
        assert not invisible, (
            "Tools the LLM receives but the Policies UI cannot see (parity failure): "
            f"{invisible}"
        )
        _reset_dynamic_registry()


# ---------------------------------------------------------------------------
# llm_visible for suppressed tools
# ---------------------------------------------------------------------------


class TestLlmVisibility:
    def test_suppressed_tools_llm_visible_false(self, tmp_path) -> None:
        """Tools in _NOUS_NATIVE_DUPLICATES should have llm_visible=False."""
        _reset_dynamic_registry()
        # run_command is in _NOUS_NATIVE_DUPLICATES (superseded by Nous-native terminal)
        snap = _fresh_store(tmp_path).snapshot()
        by_name = {e["name"]: e for e in snap["catalog"]}
        if "run_command" in by_name:
            assert by_name["run_command"]["llm_visible"] is False, (
                "run_command should be llm_visible=False (suppressed by _NOUS_NATIVE_DUPLICATES)"
            )

    def test_active_native_tools_llm_visible_true(self, tmp_path) -> None:
        """Core native tools should have llm_visible=True."""
        _reset_dynamic_registry()
        snap = _fresh_store(tmp_path).snapshot()
        by_name = {e["name"]: e for e in snap["catalog"]}
        for name in ("terminal", "web_search", "memory", "browser_navigate"):
            if name in by_name:
                assert by_name[name]["llm_visible"] is True, (
                    f"{name!r} should be llm_visible=True"
                )


# ---------------------------------------------------------------------------
# is_enabled unchanged (regression guard)
# ---------------------------------------------------------------------------


class TestIsEnabledUnchanged:
    def test_is_enabled_respects_override(self, tmp_path) -> None:
        """Explicit override persists through snapshot() enrichment unchanged."""
        store = _fresh_store(tmp_path)
        # terminal is ON by Equilibrado default
        assert store.is_enabled("terminal") is True
        store.set_tool("terminal", False)
        assert store.is_enabled("terminal") is False

    def test_is_enabled_unknown_tool_follows_preset(self, tmp_path) -> None:
        """Unknown tools (dynamic) follow the preset default — Equilibrado → ON."""
        _reset_dynamic_registry()
        store = _fresh_store(tmp_path)
        assert store.is_enabled("mcp__some__unknown_tool") is True

    def test_is_enabled_bloqueado_disables_dynamic(self, tmp_path) -> None:
        """Under BLOQUEADO preset, dynamic tools are disabled."""
        from hermes.capabilities.tool_policy import Preset
        store = _fresh_store(tmp_path)
        store.apply_preset(Preset.BLOQUEADO)
        assert store.is_enabled("mcp__any__tool") is False
