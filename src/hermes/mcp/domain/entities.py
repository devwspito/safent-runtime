"""mcp/domain/entities — McpTool entity and McpServer aggregate.

Domain layer: pure Python + stdlib only.

Invariants enforced here:
  - A tool's effective trust never exceeds its server's TrustLevel.
  - qualified_name = mcp__<slug>__<tool_name>.
  - risk / auto_executable are computed by classify_mcp_tool() at build time.
  - McpServer: ENABLED ↔ exactly 1 transport; trust_level changes only
    via explicit mutator (never self-elevated).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from hermes.capabilities.domain.ports import RiskLevel

from .tool_classifier import McpToolClassification, classify_mcp_tool
from .value_objects import McpServerId, ServerHealth, ServerSlug, Transport, TrustLevel


@dataclass(frozen=True)
class McpTool:
    """Entity representing a single tool exposed by an MCP server.

    Invariant: risk and auto_executable are SERVER-SIDE computed,
    never accepted from LLM or server description.
    """

    name: str                     # bare tool name from the MCP protocol
    description: str              # untrusted — for display only, never for routing
    slug: ServerSlug              # parent server's slug (for qualified_name)
    trust_level: TrustLevel       # capped to server's trust_level
    risk: RiskLevel
    auto_executable: bool

    @property
    def qualified_name(self) -> str:
        """mcp__<slug>__<tool_name> — the key used in CapabilityRegistry."""
        return f"mcp__{self.slug}__{self.name}"

    @classmethod
    def build(
        cls,
        *,
        name: str,
        description: str,
        slug: ServerSlug,
        trust_level: TrustLevel,
        read_only_hint: bool | None = None,
        destructive_hint: bool | None = None,
    ) -> McpTool:
        """Construct with server-side risk classification."""
        classification: McpToolClassification = classify_mcp_tool(
            name,
            read_only_hint=read_only_hint,
            destructive_hint=destructive_hint,
            trust_level=trust_level,
        )
        return cls(
            name=name,
            description=description,
            slug=slug,
            trust_level=trust_level,
            risk=classification.risk,
            auto_executable=classification.auto_executable,
        )


@dataclass
class McpServer:
    """Aggregate root for a connected MCP server.

    Lifecycle: CONNECTING → HEALTHY → DEGRADED / FAILED.
    trust_level changes only via set_trust_level() (HIGH-gated in application layer).
    restart_count is bounded; exceeding the limit transitions to FAILED (terminal).
    """

    server_id: McpServerId
    slug: ServerSlug
    transport: Transport
    trust_level: TrustLevel
    health: ServerHealth = ServerHealth.CONNECTING
    restart_count: int = 0
    tools: list[McpTool] = field(default_factory=list)

    _MAX_RESTART_COUNT: int = field(default=5, init=False, repr=False, compare=False)

    def mark_healthy(self, discovered_tools: Sequence[McpTool]) -> None:
        """Transition to HEALTHY and update tool list."""
        self.health = ServerHealth.HEALTHY
        self.tools = list(discovered_tools)

    def mark_failed(self) -> None:
        """Terminal failure — no further restart allowed."""
        self.health = ServerHealth.FAILED

    def record_restart(self) -> None:
        """Increment restart counter; transition to FAILED if limit reached."""
        self.restart_count += 1
        if self.restart_count > self._MAX_RESTART_COUNT:
            self.mark_failed()

    def set_trust_level(self, new_level: TrustLevel) -> None:
        """Mutate trust_level. Caller (application layer) must be HIGH-gated."""
        self.trust_level = new_level

    def get_tool(self, tool_name: str) -> McpTool | None:
        """Look up a tool by its bare name."""
        for t in self.tools:
            if t.name == tool_name:
                return t
        return None
