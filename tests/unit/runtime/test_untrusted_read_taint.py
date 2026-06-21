"""Taint coverage for external-content tools (CTRL-5 / prompt-injection).

red-team 2026-06-19: a tool that ingests external/untrusted content must taint the
cycle so a subsequent HIGH action is forced through HITL. Browser + Composio were
covered; MCP servers (which return arbitrary content from an external server an
attacker may control or compromise) were NOT — so acting on a poisoned MCP output
auto-executed without HITL. This pins MCP into the untrusted set alongside them.
"""

from __future__ import annotations

from hermes.domain.tool_spec import ToolRisk, ToolSpec
from hermes.runtime.tool_host import _is_untrusted_read


def _spec(name: str, tags: tuple[str, ...]) -> ToolSpec:
    # WRITE_PROPOSAL → no handler required; _is_untrusted_read only reads name/tags.
    return ToolSpec(
        name=name,
        description="x",
        parameters_schema={},
        risk=ToolRisk.WRITE_PROPOSAL,
        tags=tags,
    )


def test_mcp_output_is_untrusted() -> None:
    """The regression: an MCP tool's output taints the cycle."""
    assert _is_untrusted_read(_spec("github_create_issue", ("mcp",)), {}) is True
    assert _is_untrusted_read(_spec("replicate_run", ("mcp", "image")), {}) is True


def test_composio_and_browser_remain_untrusted() -> None:
    assert _is_untrusted_read(_spec("gmail_send", ("composio",)), {}) is True
    assert _is_untrusted_read(_spec("browser_snapshot", ("browser",)), {}) is True


def test_native_os_tool_is_trusted() -> None:
    assert _is_untrusted_read(_spec("activate_app", ("os-native",)), {}) is False
    assert _is_untrusted_read(_spec("list_apps", ()), {}) is False
