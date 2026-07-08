"""Unit tests: MCP tool spec building and broker wiring.

Covers deterministically (no real LLM, no real MCP server, no real broker):

  (a) build_mcp_tool_specs with 1 connected server + 2 tools (READ + WRITE):
      - yields 2 ToolSpecs with correct names, entity_type, risk, handler.
  (b) READ handler dispatches through broker.dispatch exactly ONCE; the
      MCP server (McpServerManager) is never called directly.
  (c) WRITE tool has handler=None and routes via _dispatch_external_write
      to the broker with entity_type='mcp' and resolvable qualified name.
  (d) Zero-connected-servers → zero specs + an INFO log line.
  (e) McpSurfaceAdapter.replay resolves server by slug when server_id is empty
      (the standard path produced by _shape_external_parameters).
  (f) _shape_external_parameters produces qualified_name in MCP payload.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from hermes.domain.tool_spec import ToolRisk, ToolSpec
from hermes.mcp.application.mcp_server_manager import McpServerManager
from hermes.mcp.domain.entities import McpServer, McpTool
from hermes.mcp.domain.value_objects import McpServerId, ServerSlug, Transport, TrustLevel

pytestmark = pytest.mark.unit

_TENANT = UUID("20000000-0000-0000-0000-000000000001")
_OPERATOR = UUID("20000000-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _NeverCalledMcpClient:
    """Client that must never be called (verifies broker-always path)."""

    async def initialize(self) -> None:
        raise AssertionError("McpClient.initialize should not be called from specs builder")

    async def list_tools(self) -> list[dict[str, Any]]:
        raise AssertionError("McpClient.list_tools should not be called from specs builder")

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(
            f"McpClient.call_tool({name!r}) should NOT be called directly — "
            "all MCP calls must route through broker.dispatch"
        )

    async def close(self) -> None: ...


@dataclass
class _RecordingBroker:
    """Fake CapabilityBrokerPort that records dispatch calls."""

    calls: list[Any] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=lambda: {"data": "ok", "is_external_content": True})

    async def dispatch(self, proposal: Any, consent_context: Any, **kwargs: Any):
        from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus  # noqa: PLC0415
        self.calls.append(proposal)
        return ExecutionOutcome(
            proposal_id=proposal.proposal_id,
            status=ExecutionStatus.EXECUTED,
            result=self.result,
        )


def _fake_consent_context() -> Any:
    from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415
    return ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)


def _make_server_manager_with_server(
    slug_str: str = "test-server",
    *,
    read_tool_name: str = "resource_list",
    write_tool_name: str = "write_file",
    trust_level: TrustLevel = TrustLevel.BUILTIN,
) -> McpServerManager:
    """Build a McpServerManager with one connected server having two tools.

    read_tool_name must end with a verb in _READ_SUFFIXES (e.g. "resource_list"
    → last segment "list" is in the set).

    Trust matters for the read/write split: BUILTIN is frictionless (BOTH tools
    become LOW+auto → READ_ONLY specs — the jail is the control), so to exercise
    the WRITE_PROPOSAL path (a write that gates via HITL) callers pass a
    non-frictionless tier. MANAGED_REMOTE is the realistic one: it classifies
    purely by NAME — read verb → LOW+auto (READ_ONLY+handler), write verb →
    LOW+not-auto (WRITE_PROPOSAL, no handler).

    The client is _NeverCalledMcpClient — any direct call to it fails the test,
    proving that build_mcp_tool_specs and the READ handler route through the broker.
    """
    slug = ServerSlug(slug_str)
    server_id = McpServerId.generate()

    read_tool = McpTool.build(
        name=read_tool_name,
        description="List resources (read-only)",
        slug=slug,
        trust_level=trust_level,
        read_only_hint=True,
        destructive_hint=False,
    )
    write_tool = McpTool.build(
        name=write_tool_name,
        description="Write file (write operation)",
        slug=slug,
        trust_level=trust_level,
        read_only_hint=False,
        destructive_hint=False,
    )

    server = McpServer(
        server_id=server_id,
        slug=slug,
        transport=Transport.stdio(["npx", "test-mcp"]),
        trust_level=trust_level,
    )
    server.mark_healthy([read_tool, write_tool])

    manager = McpServerManager(client_factory=lambda _t: _NeverCalledMcpClient())
    manager._servers[str(server_id)] = server
    manager._clients[str(server_id)] = _NeverCalledMcpClient()  # type: ignore[assignment]
    return manager


# ---------------------------------------------------------------------------
# (a) build_mcp_tool_specs produces correct ToolSpecs
# ---------------------------------------------------------------------------


class TestBuildMcpToolSpecs:
    @pytest.mark.asyncio
    async def test_yields_one_spec_per_tool(self) -> None:
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        manager = _make_server_manager_with_server("my-server")
        broker = _RecordingBroker()
        specs = await build_mcp_tool_specs(
            manager, broker=broker, consent_context=_fake_consent_context()
        )

        assert len(specs) == 2, f"Expected 2 ToolSpecs, got {len(specs)}: {[s.name for s in specs]}"

    @pytest.mark.asyncio
    async def test_qualified_names_correct(self) -> None:
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        manager = _make_server_manager_with_server(
            "my-server",
            read_tool_name="resource_list",
            write_tool_name="write_file",
        )
        broker = _RecordingBroker()
        specs = await build_mcp_tool_specs(
            manager, broker=broker, consent_context=_fake_consent_context()
        )
        names = {s.name for s in specs}
        assert "mcp__my-server__resource_list" in names
        assert "mcp__my-server__write_file" in names

    @pytest.mark.asyncio
    async def test_entity_type_is_mcp(self) -> None:
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        manager = _make_server_manager_with_server("srv")
        broker = _RecordingBroker()
        specs = await build_mcp_tool_specs(
            manager, broker=broker, consent_context=_fake_consent_context()
        )
        assert all(s.entity_type == "mcp" for s in specs)

    @pytest.mark.asyncio
    async def test_read_tool_has_read_only_risk_and_handler(self) -> None:
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        manager = _make_server_manager_with_server("srv", read_tool_name="resource_list")
        broker = _RecordingBroker()
        specs = await build_mcp_tool_specs(
            manager, broker=broker, consent_context=_fake_consent_context()
        )
        read_spec = next(s for s in specs if s.name == "mcp__srv__resource_list")
        assert read_spec.risk is ToolRisk.READ_ONLY
        assert read_spec.handler is not None, "READ tool must have a broker-dispatching handler"

    @pytest.mark.asyncio
    async def test_write_tool_has_write_proposal_risk_and_no_handler(self) -> None:
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        # MANAGED_REMOTE (not the default BUILTIN): BUILTIN is frictionless, so a
        # BUILTIN write auto-executes (LOW+auto → READ_ONLY). MANAGED_REMOTE gates
        # writes by name → WRITE_PROPOSAL, which is what this test verifies.
        manager = _make_server_manager_with_server(
            "srv", write_tool_name="write_file", trust_level=TrustLevel.MANAGED_REMOTE
        )
        broker = _RecordingBroker()
        specs = await build_mcp_tool_specs(
            manager, broker=broker, consent_context=_fake_consent_context()
        )
        write_spec = next(s for s in specs if s.name == "mcp__srv__write_file")
        assert write_spec.risk is ToolRisk.WRITE_PROPOSAL
        assert write_spec.handler is None, "WRITE tool must NOT have a handler (routes via HITL)"

    @pytest.mark.asyncio
    async def test_tags_include_mcp(self) -> None:
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        manager = _make_server_manager_with_server("srv")
        broker = _RecordingBroker()
        specs = await build_mcp_tool_specs(
            manager, broker=broker, consent_context=_fake_consent_context()
        )
        assert all("mcp" in (s.tags or ()) for s in specs)


# ---------------------------------------------------------------------------
# (b) READ handler dispatches through broker ONCE — never calls server directly
# ---------------------------------------------------------------------------


class TestReadHandlerBrokerGate:
    @pytest.mark.asyncio
    async def test_read_handler_hits_broker_exactly_once(self) -> None:
        """The READ handler closure calls broker.dispatch exactly once per invocation."""
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        manager = _make_server_manager_with_server("srv", read_tool_name="resource_list")
        broker = _RecordingBroker()
        specs = await build_mcp_tool_specs(
            manager, broker=broker, consent_context=_fake_consent_context()
        )
        read_spec = next(s for s in specs if s.name == "mcp__srv__resource_list")

        # Invoke the handler — must hit broker, never the underlying MCP client
        result = await read_spec.handler({"filter": "all"})

        assert len(broker.calls) == 1, (
            f"broker.dispatch must be called EXACTLY ONCE per READ invocation, "
            f"got {len(broker.calls)}"
        )
        assert result == broker.result

    @pytest.mark.asyncio
    async def test_read_handler_proposal_has_correct_fields(self) -> None:
        """Proposal dispatched by READ handler has correct entity_type, tool_name, args."""
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        manager = _make_server_manager_with_server("srv", read_tool_name="resource_list")
        broker = _RecordingBroker()
        specs = await build_mcp_tool_specs(
            manager, broker=broker, consent_context=_fake_consent_context()
        )
        read_spec = next(s for s in specs if s.name == "mcp__srv__resource_list")
        call_args = {"filter": "active"}

        await read_spec.handler(call_args)

        proposal = broker.calls[0]
        assert proposal.entity_type == "mcp"
        assert proposal.tool_name == "mcp__srv__resource_list"
        assert proposal.parameters["tool_name"] == "resource_list"
        assert proposal.parameters["args"] == call_args

    @pytest.mark.asyncio
    async def test_read_handler_never_calls_mcp_server_directly(self) -> None:
        """The READ handler MUST NOT bypass the broker by calling McpServerManager."""
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        # _NeverCalledMcpClient raises AssertionError if call_tool is invoked
        manager = _make_server_manager_with_server("srv", read_tool_name="resource_list")
        broker = _RecordingBroker()
        specs = await build_mcp_tool_specs(
            manager, broker=broker, consent_context=_fake_consent_context()
        )
        read_spec = next(s for s in specs if s.name == "mcp__srv__resource_list")

        # This must NOT raise AssertionError from _NeverCalledMcpClient.call_tool
        await read_spec.handler({})


# ---------------------------------------------------------------------------
# (c) WRITE routes via broker with correct entity_type and resolvable name
# ---------------------------------------------------------------------------


class TestWriteRoutesThroughBroker:
    def test_mcp_write_proposal_entity_type_is_mcp(self) -> None:
        from hermes.runtime.nous_engine import _build_external_proposal

        spec = ToolSpec(
            name="mcp__fs__write_file",
            description="write",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="mcp",
            handler=None,
        )
        proposal = _build_external_proposal(
            function_name="mcp__fs__write_file",
            function_args={"path": "/tmp/x", "content": "hello"},
            tenant_id=_TENANT,
            effective_task_id="task-w",
            spec=spec,
        )
        assert proposal.entity_type == "mcp"

    def test_mcp_write_payload_has_correct_fields(self) -> None:
        from hermes.runtime.nous_engine import _shape_external_parameters

        spec = ToolSpec(
            name="mcp__fs__write_file",
            description="write",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="mcp",
            handler=None,
        )
        params = _shape_external_parameters(
            "mcp__fs__write_file",
            {"path": "/tmp/x", "content": "hello"},
            spec,
        )
        # qualified_name enables McpSurfaceAdapter to resolve by slug
        assert params["qualified_name"] == "mcp__fs__write_file"
        assert params["tool_name"] == "write_file"
        assert params["args"] == {"path": "/tmp/x", "content": "hello"}
        # server_id is empty — resolved by slug in McpSurfaceAdapter
        assert params["server_id"] == ""

    def test_mcp_write_routes_to_broker_via_dispatch_external_write(self) -> None:
        """MCP WRITE calls _dispatch_external_write → broker, not native Nous."""
        from hermes.runtime.nous_engine import _ExternalToolCatalog, GovernedAIAgent
        import json

        spec = ToolSpec(
            name="mcp__fs__write_file",
            description="write",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="mcp",
            handler=None,
        )
        catalog = _ExternalToolCatalog((spec,))

        from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
        write_outcome = ExecutionOutcome(
            proposal_id=uuid4(),
            status=ExecutionStatus.PENDING_APPROVAL,
        )
        dispatch_calls: list[Any] = []

        def fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return write_outcome

        loop = asyncio.new_event_loop()
        fake_inner = MagicMock()
        with patch("hermes.runtime.nous_engine._import_ai_agent") as mock_import:
            mock_ai_cls = MagicMock(return_value=fake_inner)
            mock_import.return_value = mock_ai_cls
            agent = GovernedAIAgent(
                model="test/model",
                broker=MagicMock(),
                consent_context=_fake_consent_context(),
                engine_loop=loop,
                tenant_id=_TENANT,
                external_catalog=catalog,
            )
        agent._inner = fake_inner

        native_calls: list[Any] = []
        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=fake_bridge):
            with patch.object(agent, "_call_native_invoke", lambda *a, **kw: native_calls.append(1) or ""):
                agent._invoke_tool("mcp__fs__write_file", {"path": "/x"}, "task-w")

        loop.close()
        assert not native_calls, "native invoke MUST NOT be called for MCP WRITE"
        assert len(dispatch_calls) == 1
        assert dispatch_calls[0].entity_type == "mcp"


# ---------------------------------------------------------------------------
# (d) Zero-connected-servers → zero specs + INFO log
# ---------------------------------------------------------------------------


class TestZeroConnectedServers:
    @pytest.mark.asyncio
    async def test_empty_manager_returns_zero_specs(self) -> None:
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        # Manager with no connected servers
        manager = McpServerManager(client_factory=lambda _t: _NeverCalledMcpClient())
        broker = _RecordingBroker()
        specs = await build_mcp_tool_specs(
            manager, broker=broker, consent_context=_fake_consent_context()
        )

        assert specs == (), f"Expected empty tuple, got {specs}"
        assert len(broker.calls) == 0, "Broker must not be called when no servers connected"

    @pytest.mark.asyncio
    async def test_empty_manager_logs_info(self, caplog) -> None:
        from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs

        manager = McpServerManager(client_factory=lambda _t: _NeverCalledMcpClient())
        broker = _RecordingBroker()

        with caplog.at_level(logging.INFO, logger="hermes.runtime.mcp_tools"):
            await build_mcp_tool_specs(
                manager, broker=broker, consent_context=_fake_consent_context()
            )

        info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("no_connected_servers" in m for m in info_msgs), (
            f"Expected 'no_connected_servers' info log, got: {info_msgs}"
        )


# ---------------------------------------------------------------------------
# (e) McpSurfaceAdapter.replay resolves server by slug when server_id=""
# ---------------------------------------------------------------------------


class TestMcpSurfaceAdapterSlugResolution:
    @pytest.mark.asyncio
    async def test_replay_resolves_server_by_slug_when_server_id_empty(self) -> None:
        """replay() with server_id='' resolves via qualified_name slug."""
        from hermes.agents_os.domain.ports.surface_adapter_port import CapturedAction, ReplayStatus
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.mcp.infrastructure.mcp_surface_adapter import McpSurfaceAdapter

        slug = ServerSlug("my-server")
        server_id = McpServerId.generate()

        class _SucceedingClient:
            async def initialize(self) -> None: ...
            async def list_tools(self) -> list: return []
            async def call_tool(self, name: str, args: dict) -> dict:
                return {"result": f"ok_{name}"}
            async def close(self) -> None: ...

        tool = McpTool.build(
            name="resource_list",
            description="",
            slug=slug,
            trust_level=TrustLevel.BUILTIN,
            read_only_hint=True,
        )
        server = McpServer(
            server_id=server_id,
            slug=slug,
            transport=Transport.stdio(["npx", "test"]),
            trust_level=TrustLevel.BUILTIN,
        )
        server.mark_healthy([tool])

        manager = McpServerManager(client_factory=lambda _t: _SucceedingClient())
        manager._servers[str(server_id)] = server
        manager._clients[str(server_id)] = _SucceedingClient()  # type: ignore[assignment]

        adapter = McpSurfaceAdapter(server_manager=manager)
        action = CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.MCP_CALL,
            intent_desc="test",
            payload={
                "server_id": "",                                  # empty — resolve by slug
                "qualified_name": "mcp__my-server__resource_list",
                "tool_name": "resource_list",
                "args": {},
            },
        )
        outcome = await adapter.replay(action)
        assert outcome.status is ReplayStatus.EXECUTED_OK, (
            f"Expected EXECUTED_OK when server_id='' is resolved by slug, got: {outcome.status} ({outcome.error})"
        )

    @pytest.mark.asyncio
    async def test_replay_fails_closed_when_slug_not_found(self) -> None:
        """replay() with server_id='' and unknown slug returns REJECTED_BY_POLICY."""
        from hermes.agents_os.domain.ports.surface_adapter_port import CapturedAction, ReplayStatus
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.mcp.infrastructure.mcp_surface_adapter import McpSurfaceAdapter

        manager = McpServerManager(client_factory=lambda _t: _NeverCalledMcpClient())
        adapter = McpSurfaceAdapter(server_manager=manager)

        action = CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.MCP_CALL,
            intent_desc="test",
            payload={
                "server_id": "",
                "qualified_name": "mcp__unknown-server__list_resources",
                "tool_name": "resource_list",
                "args": {},
            },
        )
        outcome = await adapter.replay(action)
        assert outcome.status is ReplayStatus.REJECTED_BY_POLICY, (
            "Unknown slug must fail-closed: REJECTED_BY_POLICY"
        )

    @pytest.mark.asyncio
    async def test_replay_fails_closed_when_server_id_empty_and_no_qualified_name(self) -> None:
        """replay() with server_id='' and missing qualified_name returns REJECTED_BY_POLICY."""
        from hermes.agents_os.domain.ports.surface_adapter_port import CapturedAction, ReplayStatus
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.mcp.infrastructure.mcp_surface_adapter import McpSurfaceAdapter

        manager = McpServerManager(client_factory=lambda _t: _NeverCalledMcpClient())
        adapter = McpSurfaceAdapter(server_manager=manager)

        action = CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.MCP_CALL,
            intent_desc="test",
            payload={
                "server_id": "",
                # qualified_name absent — cannot resolve
                "tool_name": "resource_list",
                "args": {},
            },
        )
        outcome = await adapter.replay(action)
        assert outcome.status is ReplayStatus.REJECTED_BY_POLICY


# ---------------------------------------------------------------------------
# (f) _shape_external_parameters includes qualified_name in MCP payload
# ---------------------------------------------------------------------------


class TestShapeExternalParametersMcpQualifiedName:
    def test_qualified_name_in_mcp_payload(self) -> None:
        from hermes.runtime.nous_engine import _shape_external_parameters

        # Use WRITE_PROPOSAL (handler=None) for a simple shape test that
        # doesn't require a real broker handler — we test shape only here.
        spec = ToolSpec(
            name="mcp__srv__resource_list",
            description="list",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="mcp",
            handler=None,
        )
        params = _shape_external_parameters("mcp__srv__resource_list", {"x": 1}, spec)
        assert params["qualified_name"] == "mcp__srv__resource_list"
        assert params["tool_name"] == "resource_list"
        assert params["server_id"] == ""
        assert params["args"] == {"x": 1}

    def test_composio_payload_unchanged(self) -> None:
        """Composio path is not affected by the MCP qualified_name addition."""
        from hermes.runtime.nous_engine import _shape_external_parameters

        spec = ToolSpec(
            name="gmail_send_email",
            description="send",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="composio",
            handler=None,
        )
        params = _shape_external_parameters("gmail_send_email", {"q": "inbox"}, spec)
        assert "qualified_name" not in params
        assert params["slug"] == "GMAIL_SEND_EMAIL"
        assert params["params"] == {"q": "inbox"}
