"""AgentAccessScope aggregate — per-agent native-tool access scope.

Enterprise governance, Fase 2 Phase 1 (runtime-only foundation; no cloud/
config-sync in this phase). A sibling of AgentCapabilityBinding
(agent_capability_binding.py), NOT an extension of it: that aggregate assigns
GLOBAL capabilities (kind ∈ {platform, skill}) to an agent and deliberately
forbids integrations. AgentAccessScope governs a DIFFERENT axis entirely —
which NATIVE Nous tools (terminal, browser_*, read_file, write_file,
computer_use, ...) an agent may invoke at all — plus two fields carried for a
LATER phase (policy_overlay: per-tool override; views: per-agent view
entitlement). Neither is enforced yet; this phase only wires the allow-set.

Domain layer — pure Python, zero infra dependencies.

Invariants:
- scope_id/tenant_id/agent_id/updated_by are required.
- native_tools is a frozenset[str] allow-set over native Nous tool names.
- policy_overlay is a dict, views is a tuple[str, ...] — carried, not yet
  resolved/enforced (no-op today; a later phase adds the resolver + router).
- enforced defaults to False: with no cloud policy pushed for this agent, the
  scope governs NOTHING — every native tool call passes, identical to the
  pre-Fase-2 behaviour (zero regression for every existing/local install).
  Only enforced=True scopes restrict native tool calls to native_tools.
- cerebro_unrestricted defaults to True: the ONLY knob that parks the CEO/
  Cerebro omnipotence bypass. Deciding WHETHER a given agent_id is "the CEO"
  is an application-level concern (compare against DEFAULT_AGENT_ID) that
  lives in the caller (nous_engine / security_hook), never in this aggregate.
- updated_by is the sender_uid (D-Bus peer cred), NEVER from payload —
  mirrors AgentCapabilityBinding.bound_by.
- authorized_mcp_servers is a frozenset[str] of MCP server slugs the agent's
  SIGNED bundle authorizes (2026-07-07: confused-deputy fix). config-sync
  (uid=hermes) is D-Bus-denied BindCapabilityToAgent by design, so it cannot
  create an AgentCapabilityBinding for MCP servers; it instead lands the
  bundle's MCP allow-set HERE via the (allowed) set_agent_access_scope verb.
  nous_engine._filter_mcp_skill reads this set as an ADDITIONAL source for
  its fail-closed MCP admission — a slug absent from BOTH this set and the
  agent's capability bindings is still denied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4


@dataclass
class AgentAccessScope:
    """Per-agent native-tool access scope.

    `enforced=False` (default) means this scope does not govern anything yet
    (no cloud policy pushed for this agent) — every native tool call passes,
    identical to the pre-Fase-2 behaviour. Setting `enforced=True` and
    populating `native_tools` turns this into an allow-list floor enforced by
    the security hook (see runtime/security_hook.py).
    """

    scope_id: str
    tenant_id: str
    agent_id: str
    updated_by: int
    native_tools: frozenset[str] = field(default_factory=frozenset)
    policy_overlay: dict = field(default_factory=dict)
    views: tuple[str, ...] = ()
    cerebro_unrestricted: bool = True
    enforced: bool = False
    managed_by: str | None = None
    # MCP server slugs the bundle authorizes for this agent (module docstring).
    authorized_mcp_servers: frozenset[str] = field(default_factory=frozenset)
    # Per-role approval tier (2026-07-05; INERT for routing since Fase 2 Phase
    # 4c's TOTP-keyed model, 2026-07-06): "coordinator" | "standard" no longer
    # changes WHO resolves an approval — approval_router.route() now decides
    # purely on tool_delicacy.is_mfa_required(tool), the same for every tier.
    # Cloud-authored, lands from the signed bundle. Kept for back-compat/
    # observability only — never widens the floor.
    approval_tier: str = "standard"
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def __post_init__(self) -> None:
        if not self.scope_id:
            raise ValueError("AgentAccessScope.scope_id cannot be empty")
        if not self.tenant_id:
            raise ValueError("AgentAccessScope.tenant_id cannot be empty")
        if not self.agent_id:
            raise ValueError("AgentAccessScope.agent_id cannot be empty")
        if not isinstance(self.native_tools, frozenset):
            raise TypeError("AgentAccessScope.native_tools must be a frozenset[str]")
        if not isinstance(self.policy_overlay, dict):
            raise TypeError("AgentAccessScope.policy_overlay must be a dict")
        if not isinstance(self.views, tuple):
            raise TypeError("AgentAccessScope.views must be a tuple[str, ...]")
        if not isinstance(self.authorized_mcp_servers, frozenset):
            raise TypeError(
                "AgentAccessScope.authorized_mcp_servers must be a frozenset[str]"
            )

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        agent_id: str,
        updated_by: int,
        native_tools: frozenset[str] = frozenset(),
        policy_overlay: dict | None = None,
        views: tuple[str, ...] = (),
        cerebro_unrestricted: bool = True,
        enforced: bool = False,
        managed_by: str | None = None,
        approval_tier: str = "standard",
        authorized_mcp_servers: frozenset[str] = frozenset(),
    ) -> AgentAccessScope:
        """Factory: create a new scope with a generated id."""
        return cls(
            scope_id=uuid4().hex,
            tenant_id=tenant_id,
            agent_id=agent_id,
            updated_by=updated_by,
            native_tools=frozenset(native_tools),
            policy_overlay=dict(policy_overlay or {}),
            views=tuple(views),
            cerebro_unrestricted=cerebro_unrestricted,
            enforced=enforced,
            managed_by=managed_by,
            approval_tier=approval_tier,
            authorized_mcp_servers=frozenset(authorized_mcp_servers),
        )

    def allows_native_tool(self, tool_name: str) -> bool:
        """Whether *tool_name* passes this scope's native-tool floor.

        `enforced=False` allows everything (this scope does not govern yet —
        zero regression). `enforced=True` restricts to the `native_tools`
        allow-set. Whether the CALLING agent is exempt from enforcement
        entirely (the CEO/Cerebro omnipotence bypass) is decided by the
        caller, not here — see module docstring.
        """
        if not self.enforced:
            return True
        return tool_name in self.native_tools

    def to_dict(self) -> dict:
        """Serialize for D-Bus JSON transport (no PII, no credentials)."""
        return {
            "scope_id": self.scope_id,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "native_tools": sorted(self.native_tools),
            "policy_overlay": dict(self.policy_overlay),
            "views": list(self.views),
            "cerebro_unrestricted": self.cerebro_unrestricted,
            "enforced": self.enforced,
            "updated_by": self.updated_by,
            "managed_by": self.managed_by,
            "approval_tier": self.approval_tier,
            "authorized_mcp_servers": sorted(self.authorized_mcp_servers),
            "updated_at": self.updated_at.isoformat(),
        }
