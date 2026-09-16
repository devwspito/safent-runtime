"""Catalog discovery is a native read, not an unclassified or write bridge."""
from unittest.mock import MagicMock, patch

import pytest

from hermes.runtime.nous_engine import GovernedAIAgent, _ExternalToolCatalog
from hermes.runtime.nous_tool_risk_map import NousRisk, classify_nous_tool

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["tool_search", "tool_describe"])
def test_catalog_read_uses_real_native_route(name):
    agent = object.__new__(GovernedAIAgent)
    agent._external_catalog = _ExternalToolCatalog(())
    agent._read_external_content = False
    agent._call_native_invoke = MagicMock(return_value='{"tools":[]}')
    assert classify_nous_tool(name) is NousRisk.READ
    with patch("hermes.runtime.managed_llm_bootstrap.assert_process_admission"):
        result = agent._invoke_tool(name, {"query": "campaign drafts"}, "task")
    assert result == '{"tools":[]}'
    assert agent._call_native_invoke.call_args.args[0] == name


def test_generic_tool_call_does_not_receive_blanket_native_permission():
    # Hermes unwraps a valid tool_call before the hook/invoke, preserving the
    # target's classification. An unresolved bridge must still fail closed.
    assert classify_nous_tool("tool_call") is None
    agent = object.__new__(GovernedAIAgent)
    agent._external_catalog = _ExternalToolCatalog(())
    agent._call_native_invoke = MagicMock()
    with patch("hermes.runtime.managed_llm_bootstrap.assert_process_admission"):
        result = agent._invoke_tool("tool_call", {"name": "unknown_write"}, "task")
    assert "BLOCKED" in result
    agent._call_native_invoke.assert_not_called()
