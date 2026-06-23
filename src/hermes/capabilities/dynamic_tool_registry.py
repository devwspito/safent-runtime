"""Live registry of dynamic tools (MCP + Composio) currently loaded into the LLM.

Purpose
-------
_tools_source in __main__ builds MCP and Composio ToolSpecs per cycle and
injects them into the LLM schema.  Those tools are *invisible* to the static
TOOL_CATALOG in tool_policy.py — the Policies UI cannot list or toggle them.

This module provides a process-scoped singleton that _tools_source publishes
into after each build, and that ToolPolicyStore.snapshot() reads from to
include dynamic tools in the enriched catalog.

Design constraints
------------------
- Single-writer (the daemon's async loop via _tools_source).
- Multiple-reader (snapshot() may be called from the shell_server HTTP handler
  thread, which runs in the same asyncio loop via asyncio.run_coroutine_threadsafe).
  The GIL plus the atomic nature of simple dict/list assignment is sufficient for
  this single-producer use case; no lock is required.
- Zero persistence: the registry resets at daemon restart.  That is correct —
  MCP/Composio connections are ephemeral per session.
- No circular imports: this module imports only stdlib (typing, dataclasses).
  tool_policy.py imports from here; __main__.py imports from here.

Registered tool entry
---------------------
  name    : qualified tool name (e.g. "mcp__ruflo__web_search", "gmail_send_email")
  origin  : "mcp" | "composio"
  llm_visible : True (always — these are the specs the LLM currently sees)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Origin = Literal["native", "capability", "mcp", "composio"]


@dataclass(frozen=True, slots=True)
class DynamicToolEntry:
    """Metadata for a single dynamic tool currently registered in the LLM schema."""

    name: str
    origin: Literal["mcp", "composio"]


class DynamicToolRegistry:
    """In-memory registry of currently-active MCP and Composio tools.

    Lifecycle: one instance per daemon process (process-scoped singleton via
    the module-level `_registry` object).  Reset to empty at daemon restart.
    """

    def __init__(self) -> None:
        self._tools: dict[str, DynamicToolEntry] = {}

    def publish(self, tools: tuple[DynamicToolEntry, ...]) -> None:
        """Replace the current dynamic tool set with the fresh build.

        Called by _tools_source after each per-cycle spec build.  The full set
        is replaced atomically (dict assignment is GIL-safe for single writer).
        Calling with an empty tuple clears the registry (e.g. all MCP servers
        disconnected, no Composio apps active).
        """
        self._tools = {e.name: e for e in tools}

    def all(self) -> tuple[DynamicToolEntry, ...]:
        """Return the current snapshot of registered dynamic tools."""
        return tuple(self._tools.values())

    def get(self, name: str) -> DynamicToolEntry | None:
        """Return the entry for a named tool, or None if not registered."""
        return self._tools.get(name)

    def __len__(self) -> int:
        return len(self._tools)


# Process-scoped singleton — import and use directly.
# One instance is shared across the daemon and all HTTP handlers.
_registry = DynamicToolRegistry()


def get_dynamic_tool_registry() -> DynamicToolRegistry:
    """Return the process-scoped singleton DynamicToolRegistry."""
    return _registry
