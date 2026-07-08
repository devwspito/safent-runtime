"""MANAGED_REMOTE taint-propagation — verifies CTRL-5 already covers this tier.

Investigation (013-P1 MANAGED_REMOTE follow-up): mcp_broker_handler.py claims
"taint propagation handled by CapturingToolHost via the 'mcp' tag on the
ToolSpec". This module PROVES that claim holds for a MANAGED_REMOTE server
specifically, and that it needed ZERO additional wiring.

Root cause traced in tool_host.py:_is_untrusted_read(): every ToolSpec built
from an MCP tool carries tags=("mcp",) UNCONDITIONALLY — mcp_tool_specs.py's
_mcp_tool_to_spec() never threads trust_level into the tag. _is_untrusted_read
checks tags, not trust_level, so a MANAGED_REMOTE server's READ result is
tainted by the exact same rule as a BUILTIN or USER_ADDED server's — no
trust-level branch exists (or is needed) in the taint path.

Consequence proven end-to-end here: a safent-control READ this cycle sets
CapturedRound.ingested_untrusted_content=True, which the engine folds into
CycleOutput.read_external_content → ConsentContext.derived_from_untrusted_content
for the REST of the cycle — so a poisoned safent-control response cannot
silently drive a subsequent WRITE (see tests/security/test_mcp_broker_route.py
TestMcpManagedRemoteWriteTaint* for the broker-side half of this control).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.domain.tool_spec import ToolRisk
from hermes.mcp.application.mcp_server_manager import McpServerManager
from hermes.mcp.domain.entities import McpServer, McpTool
from hermes.mcp.domain.value_objects import McpServerId, ServerSlug, Transport, TrustLevel
from hermes.runtime.tool_host import CapturingToolHost

pytestmark = pytest.mark.unit

_TENANT = UUID("30000000-0000-0000-0000-000000000001")
_OPERATOR = UUID("30000000-0000-0000-0000-000000000002")


@dataclass
class _RecordingBroker:
    """Fake CapabilityBrokerPort — always EXECUTED, records dispatch calls."""

    calls: list[Any] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=lambda: {"agents": ["a", "b"]})

    async def dispatch(self, proposal: Any, consent_context: Any, **kwargs: Any):
        from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus  # noqa: PLC0415
        self.calls.append(proposal)
        return ExecutionOutcome(
            proposal_id=proposal.proposal_id, status=ExecutionStatus.EXECUTED, result=self.result,
        )


def _fake_consent_context() -> Any:
    from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415
    return ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)


def _managed_remote_server_manager(slug_str: str = "safent-control") -> McpServerManager:
    """One connected MANAGED_REMOTE server with a read tool + a write tool."""
    slug = ServerSlug(slug_str)
    server_id = McpServerId.generate()

    read_tool = McpTool.build(
        name="list_agents",
        description="List the tenant's agents",
        slug=slug,
        trust_level=TrustLevel.MANAGED_REMOTE,
    )
    write_tool = McpTool.build(
        name="create_employee",
        description="Provision a new employee agent",
        slug=slug,
        trust_level=TrustLevel.MANAGED_REMOTE,
    )
    assert read_tool.auto_executable is True
    assert write_tool.auto_executable is False

    server = McpServer(
        server_id=server_id,
        slug=slug,
        transport=Transport.stdio(["npx", "safent-control-mcp"]),
        trust_level=TrustLevel.MANAGED_REMOTE,
    )
    server.mark_healthy([read_tool, write_tool])

    class _UnusedClient:
        async def initialize(self) -> None: ...
        async def list_tools(self) -> list: return []
        async def call_tool(self, name: str, args: dict) -> dict: return {}
        async def close(self) -> None: ...

    manager = McpServerManager(client_factory=lambda _t: _UnusedClient())
    manager._servers[str(server_id)] = server
    manager._clients[str(server_id)] = _UnusedClient()  # type: ignore[assignment]
    return manager


def _make_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class TestManagedRemoteToolSpecCarriesMcpTag:
    """The ToolSpec built for a MANAGED_REMOTE tool carries tags=('mcp',) —
    the same tag every other trust tier gets. This is what makes the taint
    rule below apply uniformly, with no MANAGED_REMOTE-specific branch.
    """

    @pytest.mark.asyncio
    async def test_read_spec_has_mcp_tag(self) -> None:
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        manager = _managed_remote_server_manager()
        specs = await build_mcp_tool_specs(
            manager, broker=_RecordingBroker(), consent_context=_fake_consent_context()
        )
        read_spec = next(s for s in specs if s.name == "mcp__safent-control__list_agents")
        assert read_spec.tags == ("mcp",)
        assert read_spec.risk is ToolRisk.READ_ONLY

    @pytest.mark.asyncio
    async def test_write_spec_has_mcp_tag_and_no_handler(self) -> None:
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        manager = _managed_remote_server_manager()
        specs = await build_mcp_tool_specs(
            manager, broker=_RecordingBroker(), consent_context=_fake_consent_context()
        )
        write_spec = next(s for s in specs if s.name == "mcp__safent-control__create_employee")
        assert write_spec.tags == ("mcp",)
        assert write_spec.risk is ToolRisk.WRITE_PROPOSAL
        assert write_spec.handler is None


class TestManagedRemoteReadTaintsTheCycle:
    """CapturingToolHost taints the round for a MANAGED_REMOTE read — proving
    no additional wiring was needed beyond the existing 'mcp' tag rule.
    """

    @pytest.mark.asyncio
    async def test_safent_control_read_marks_round_untrusted(self) -> None:
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        manager = _managed_remote_server_manager()
        broker = _RecordingBroker()
        specs = await build_mcp_tool_specs(
            manager, broker=broker, consent_context=_fake_consent_context()
        )
        read_spec = next(s for s in specs if s.name == "mcp__safent-control__list_agents")

        host = CapturingToolHost(specs=(read_spec,), tenant_id=_TENANT)
        round_result = await host.process_round(
            [_make_call("c1", "mcp__safent-control__list_agents", {})]
        )

        assert round_result.ingested_untrusted_content is True, (
            "A MANAGED_REMOTE MCP read result must taint the round — a poisoned "
            "safent-control response must not be trusted context for the rest "
            "of the cycle (CTRL-5)."
        )
        assert len(round_result.tool_results) == 1
        assert len(broker.calls) == 1, "the read must still route through broker.dispatch exactly once"
