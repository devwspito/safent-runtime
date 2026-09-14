"""Connected external tools: the FULL catalog is registered and gate-classified;
intent retrieval only narrows what the model sees DIRECTLY.

Regression for the owner's chat of 2026-09-14: `mcp__safent-ads__list_campaign_drafts`
was not among the turn's top-K, so the runtime never registered it in the Nous
registry — the model could not see it, `tool_call` answered "not a deferrable tool"
and `tool_search` could not list it. A tool the model cannot see must still be
discoverable and callable through Hermes's bridge.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

from hermes.domain.tool_spec import ToolRisk, ToolSpec
from hermes.runtime import __main__ as runtime_main
from hermes.runtime.conversation_task_registry import (
    get_visible_external_names,
    set_current_message,
    set_visible_external_names,
)
from hermes.runtime.nous_engine import (
    _ensure_bridge_tools,
    _sync_agent_tools_with_external,
    _visible_external_specs,
)


def _mcp(tool: str) -> ToolSpec:
    return ToolSpec(
        name=f"mcp__safent-ads__{tool}",
        description=f"safent-ads {tool}",
        parameters_schema={"type": "object", "properties": {}},
        risk=ToolRisk.READ_ONLY,
        entity_type="mcp",
        handler=MagicMock(),
    )


class _FakeIndex:
    def __init__(self, picked):
        self._picked = picked

    def retrieve(self, _msg, integration, k):  # noqa: ARG002
        return self._picked


class TestRetrievalNarrowsVisibilityNotTheCatalog:
    def test_top_k_is_stamped_and_the_list_keeps_every_tool(self, monkeypatch) -> None:
        specs = [_mcp("list_businesses"), _mcp("get_brand_kit"), _mcp("list_campaign_drafts")]
        monkeypatch.setattr(runtime_main, "_tool_index", lambda: _FakeIndex(specs[:2]))
        set_current_message("crea dos borradores de campaña")

        runtime_main._stamp_visible_integration(specs)

        assert len(specs) == 3, "the catalog handed to the engine must stay complete"
        assert get_visible_external_names() == frozenset(
            {"mcp__safent-ads__list_businesses", "mcp__safent-ads__get_brand_kit"}
        )

    def test_no_message_means_everything_visible(self, monkeypatch) -> None:
        specs = [_mcp("a"), _mcp("b")]
        monkeypatch.setattr(runtime_main, "_tool_index", lambda: _FakeIndex(specs[:1]))
        set_visible_external_names(frozenset({"stale"}))
        set_current_message("")

        runtime_main._stamp_visible_integration(specs)

        assert get_visible_external_names() is None

    def test_retrieval_failure_is_fail_soft(self, monkeypatch) -> None:
        def _boom():
            raise RuntimeError("embedder down")

        monkeypatch.setattr(runtime_main, "_tool_index", _boom)
        set_current_message("hola")
        specs = [_mcp("a")]

        runtime_main._stamp_visible_integration(specs)

        assert get_visible_external_names() is None
        assert len(specs) == 1


class TestVisibleSubset:
    def test_only_stamped_names_are_visible(self) -> None:
        specs = (_mcp("a"), _mcp("b"), _mcp("c"))
        set_visible_external_names(frozenset({"mcp__safent-ads__b"}))

        assert _visible_external_specs(specs) == (specs[1],)

    def test_without_stamp_the_full_tuple_is_visible(self) -> None:
        specs = (_mcp("a"), _mcp("b"))
        set_visible_external_names(None)

        assert _visible_external_specs(specs) == specs


def _fake_hermes_bridge(monkeypatch) -> None:
    """Stand in for hermes-agent's tools.tool_search (absent in unit tests)."""
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []  # package marker so submodule imports resolve via sys.modules
    ts = types.ModuleType("tools.tool_search")
    ts.BRIDGE_TOOL_NAMES = frozenset({"tool_search", "tool_describe", "tool_call"})
    ts.bridge_tool_schemas = lambda _deferred_count: [
        {"type": "function", "function": {"name": n, "parameters": {"type": "object"}}}
        for n in ("tool_search", "tool_describe", "tool_call")
    ]
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.tool_search", ts)
    monkeypatch.delitem(sys.modules, "tools.registry", raising=False)


class TestColdCycleBridge:
    def test_bridge_tools_are_added_when_externals_stay_hidden(self, monkeypatch) -> None:
        _fake_hermes_bridge(monkeypatch)
        kept: list = [{"type": "function", "function": {"name": "read_file"}}]

        added = _ensure_bridge_tools(kept, deferred_count=40)

        assert added == 3
        names = {t["function"]["name"] for t in kept}
        assert {"tool_search", "tool_describe", "tool_call"} <= names

    def test_bridge_is_not_duplicated(self, monkeypatch) -> None:
        _fake_hermes_bridge(monkeypatch)
        kept: list = [{"type": "function", "function": {"name": "tool_search"}}]

        assert _ensure_bridge_tools(kept, deferred_count=5) == 0
        assert len(kept) == 1

    def test_without_hermes_agent_nothing_breaks(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "tools", None)  # import fails -> fail-soft
        kept: list = []

        assert _ensure_bridge_tools(kept, deferred_count=5) == 0
        assert kept == []

    def test_sync_exposes_visible_specs_plus_bridge_for_the_rest(self, monkeypatch) -> None:
        _fake_hermes_bridge(monkeypatch)
        inner = types.SimpleNamespace(tools=[], valid_tool_names=set())
        agent = types.SimpleNamespace(_inner=inner)
        visible = (_mcp("list_businesses"),)

        _sync_agent_tools_with_external(agent, visible, deferred_count=39)

        names = {t["function"]["name"] for t in inner.tools}
        assert "mcp__safent-ads__list_businesses" in names
        assert {"tool_search", "tool_describe", "tool_call"} <= names
        assert inner.valid_tool_names == names

    def test_sync_without_hidden_externals_adds_no_bridge(self, monkeypatch) -> None:
        _fake_hermes_bridge(monkeypatch)
        inner = types.SimpleNamespace(tools=[], valid_tool_names=set())
        agent = types.SimpleNamespace(_inner=inner)

        _sync_agent_tools_with_external(agent, (_mcp("a"),), deferred_count=0)

        assert {t["function"]["name"] for t in inner.tools} == {"mcp__safent-ads__a"}
