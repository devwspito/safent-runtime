"""Shared identity for companion-server tools (parity fix, items 2.1/2.5).

A companion (today exactly `safent-ads`, see
`hermes.shell_server.companions._COMPANION_SLUGS`) is a sibling container the
local installer provisions — the owner's ONLY path to those ~100 tools,
unlike Claude Code/Codex which receive their full catalog directly. Hermes's
own per-turn top-K intent narrowing (`hermes.runtime.__main__._stamp_visible_
integration`) must never hide a companion's tools from the model, and the
Ads chat guidance must attach whenever the companion is registered — both
keyed on this one prefix so the two call sites can never drift apart.

The MCP bridge namespaces every tool as `mcp__<server-slug>__<tool>`; matching
that prefix here (runtime layer) is cheaper and more local than importing the
root-only companion registry (`hermes.shell_server.companions`, meant for the
install/network trust boundary, not tool-catalog code).
"""

from __future__ import annotations

ADS_COMPANION_TOOL_PREFIX = "mcp__safent-ads__"


def is_companion_tool(name: str) -> bool:
    """True for any tool namespaced under an always-visible companion server."""
    return name.startswith(ADS_COMPANION_TOOL_PREFIX)
