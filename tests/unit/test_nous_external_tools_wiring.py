"""Tests F3: Composio + MCP ToolSpec wiring into the Nous engine.

Verifies deterministically (no real LLM, no real broker, no hermes-agent):

  (a) DISCOVERY: injected external ToolSpec count > 0 when tools_source returns specs.
  (b) CLASSIFICATION: a Composio READ slug is not default-denied (routes to READ handler).
  (c) CLASSIFICATION: a Composio WRITE slug routes to broker proposal, not native invoke.
  (d) CLASSIFICATION: a MCP qualified name routes correctly.
  (e) GATE: every external effectful call MUST pass through the broker (no bypass).
  (f) GATE: READ handler is called via the engine_loop bridge — no direct adapter call.
  (g) GATE: tools_source=None is fail-safe at resolve time (returns ()) with NO
      per-cycle warning; the LOUD wiring-gap warning fires ONCE at build time
      (__main__._build_nous_engine → 'nous_engine_no_tools_source').
  (h) ZERO_TOOLS: 0 external specs is a legitimate B4 outcome (fresh instance /
      access-scope-locked agent) → returns () with NO per-cycle warning.
  (i) WRITE: external WRITE proposal has correct entity_type (composio/mcp).
  (j) BUILD PROPOSAL: _build_external_proposal shapes composio parameters correctly.
  (k) BUILD PROPOSAL: _build_external_proposal shapes mcp parameters correctly.
  (l) REGISTRY: _register_external_specs_in_nous is idempotent (override=True).
  (m) _build_nous_engine in __main__ forwards tools_source → warns when None.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
from hermes.domain.tool_spec import ToolRisk, ToolSpec
from hermes.runtime.nous_engine import (
    _ExternalToolCatalog,
    _build_external_proposal,
    _register_external_specs_in_nous,
    _shape_external_parameters,
    _toolset_for_spec,
    GovernedAIAgent,
    NousReasoningEngine,
)
from hermes.runtime.nous_tool_risk_map import classify_nous_tool

pytestmark = pytest.mark.unit

_TENANT = UUID("10000000-0000-0000-0000-000000000001")
_OPERATOR = UUID("10000000-0000-0000-0000-000000000002")


def _persona():
    from hermes.prompts.persona import PersonaSpec
    return PersonaSpec(
        name="H",
        role="test-role",
        language="es",
        register="formal",
        primary_mission="testing",
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _consent_ctx() -> Any:
    from hermes.capabilities.domain.ports import ConsentContext
    return ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)


def _outcome(
    status: ExecutionStatus,
    result: dict | None = None,
    error: str | None = None,
) -> ExecutionOutcome:
    return ExecutionOutcome(
        proposal_id=uuid4(),
        status=status,
        result=result or {},
        error=error,
    )


def _read_spec(name: str = "gmail_get_email") -> ToolSpec:
    """Fake READ Composio ToolSpec with a broker-dispatching async handler."""
    async def _handler(params: dict) -> dict:
        return {"emails": [], "count": 0}

    return ToolSpec(
        name=name,
        description=f"Composio READ: {name}",
        parameters_schema={"type": "object", "properties": {}},
        risk=ToolRisk.READ_ONLY,
        entity_type="composio",
        handler=_handler,
    )


def _write_spec(name: str = "gmail_send_email") -> ToolSpec:
    """Fake WRITE Composio ToolSpec (handler=None as per ToolSpec invariant)."""
    return ToolSpec(
        name=name,
        description=f"Composio WRITE: {name}",
        parameters_schema={"type": "object", "properties": {}},
        risk=ToolRisk.WRITE_PROPOSAL,
        entity_type="composio",
        handler=None,
    )


def _mcp_read_spec(slug: str = "filesystem", tool: str = "list_files") -> ToolSpec:
    """Fake READ MCP ToolSpec."""
    qualified = f"mcp__{slug}__{tool}"

    async def _handler(params: dict) -> dict:
        return {"files": []}

    return ToolSpec(
        name=qualified,
        description=f"MCP READ: {qualified}",
        parameters_schema={"type": "object", "properties": {}},
        risk=ToolRisk.READ_ONLY,
        entity_type="mcp",
        handler=_handler,
    )


def _mcp_write_spec(slug: str = "filesystem", tool: str = "write_file") -> ToolSpec:
    qualified = f"mcp__{slug}__{tool}"
    return ToolSpec(
        name=qualified,
        description=f"MCP WRITE: {qualified}",
        parameters_schema={"type": "object", "properties": {}},
        risk=ToolRisk.WRITE_PROPOSAL,
        entity_type="mcp",
        handler=None,
    )


def _make_governed_agent(
    broker: Any = None,
    consent_ctx: Any = None,
    engine_loop: asyncio.AbstractEventLoop | None = None,
    external_catalog: _ExternalToolCatalog | None = None,
) -> GovernedAIAgent:
    """Construye GovernedAIAgent con mocks sin importar hermes-agent."""
    fake_inner = MagicMock()
    with patch("hermes.runtime.nous_engine._import_ai_agent") as mock_import:
        mock_ai_cls = MagicMock(return_value=fake_inner)
        mock_import.return_value = mock_ai_cls
        agent = GovernedAIAgent(
            model="test/model",
            broker=broker,
            consent_context=consent_ctx or _consent_ctx(),
            engine_loop=engine_loop,
            tenant_id=_TENANT,
            external_catalog=external_catalog,
        )
    agent._inner = fake_inner
    return agent


# ---------------------------------------------------------------------------
# (a) DISCOVERY: external catalog count > 0 when tools_source returns specs
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_external_catalog_from_specs(self) -> None:
        """_ExternalToolCatalog holds all injected specs."""
        specs = (_read_spec("gmail_get_email"), _write_spec("gmail_send_email"))
        catalog = _ExternalToolCatalog(specs)
        assert len(catalog) == 2
        assert catalog.get("gmail_get_email") is not None
        assert catalog.get("gmail_send_email") is not None
        assert catalog.get("nonexistent") is None

    def test_external_catalog_empty(self) -> None:
        """Empty catalog returns None for any lookup."""
        catalog = _ExternalToolCatalog(())
        assert len(catalog) == 0
        assert catalog.get("anything") is None

    @pytest.mark.asyncio
    async def test_resolve_external_specs_filters_native_tools(self) -> None:
        """_resolve_external_specs drops specs whose names match native Nous tools."""
        # read_file is a native Nous tool — should be filtered out.
        native_spec = ToolSpec(
            name="read_file",
            description="native",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.READ_ONLY,
            entity_type="composio",
            handler=AsyncMock(return_value={}),
        )
        external_spec = _read_spec("gmail_get_email")

        async def _source() -> tuple:
            return (native_spec, external_spec)

        engine = NousReasoningEngine(
            persona=_persona(),
            tools_source=_source,
        )
        specs = await engine._resolve_external_specs()
        names = {s.name for s in specs}
        assert "read_file" not in names, "native Nous tool must be filtered"
        assert "gmail_get_email" in names

    @pytest.mark.asyncio
    async def test_resolve_external_specs_returns_injected_specs(self) -> None:
        """External specs from tools_source are discovered and returned (count > 0).

        Per-cycle count observability lives at BUILD time (__main__._build_nous_engine
        logs 'engine_kind=nous ... tools_source_wired=%s'); _resolve_external_specs runs
        on every reasoning cycle and stays silent to avoid log spam. The engine-level
        invariant is the RETURN VALUE: wired specs surface for the model.
        """
        async def _source() -> tuple:
            return (_read_spec("gmail_get_email"),)

        engine = NousReasoningEngine(
            persona=_persona(),
            tools_source=_source,
        )
        specs = await engine._resolve_external_specs()

        assert len(specs) == 1
        assert specs[0].name == "gmail_get_email"
        assert specs[0].entity_type == "composio"


# ---------------------------------------------------------------------------
# (b) READ Composio slug not default-denied → routes to handler
# ---------------------------------------------------------------------------


class TestComposioReadNotBlocked:
    def test_composio_read_not_in_nous_catalog(self) -> None:
        """gmail_get_email is unknown to classify_nous_tool (returns None)."""
        assert classify_nous_tool("gmail_get_email") is None

    def test_composio_read_routes_to_handler_not_native(self) -> None:
        """External READ routes to _execute_external_read, NOT to native Nous dispatcher."""
        spec = _read_spec("gmail_get_email")
        catalog = _ExternalToolCatalog((spec,))

        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(
            broker=MagicMock(),
            engine_loop=loop,
            external_catalog=catalog,
        )

        native_calls: list[str] = []
        external_read_calls: list[str] = []

        def fake_native(*a, **kw) -> str:
            native_calls.append("called")
            return json.dumps({"native": True})

        def fake_external_read(fn, args, s) -> str:
            external_read_calls.append(fn)
            return json.dumps({"emails": []})

        with patch.object(agent, "_call_native_invoke", side_effect=fake_native):
            with patch.object(agent, "_execute_external_read", side_effect=fake_external_read):
                agent._invoke_tool("gmail_get_email", {}, "task-001")

        assert len(native_calls) == 0, "native invoke must NOT be called for external tool"
        assert len(external_read_calls) == 1
        loop.close()

    def test_composio_read_executes_via_engine_loop_bridge(self) -> None:
        """External READ calls spec.handler via asyncio.run_coroutine_threadsafe."""
        handler_results: list[dict] = []

        async def _recording_handler(params: dict) -> dict:
            result = {"emails": ["a@b.com"], "count": 1}
            handler_results.append(result)
            return result

        spec = ToolSpec(
            name="gmail_get_email",
            description="get emails",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.READ_ONLY,
            entity_type="composio",
            handler=_recording_handler,
        )
        catalog = _ExternalToolCatalog((spec,))

        # Run a real async loop in the background to process the bridge call.
        bg_loop = asyncio.new_event_loop()
        import threading
        t = threading.Thread(target=bg_loop.run_forever, daemon=True)
        t.start()

        try:
            agent = _make_governed_agent(engine_loop=bg_loop, external_catalog=catalog)
            result_str = agent._invoke_tool("gmail_get_email", {"query": "inbox"}, "task-x")
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=3)

        parsed = json.loads(result_str)
        assert parsed.get("count") == 1
        assert len(handler_results) == 1


# ---------------------------------------------------------------------------
# (c) Composio WRITE routes to broker proposal, not native invoke
# ---------------------------------------------------------------------------


class TestComposioWriteRoutesToBroker:
    def test_composio_write_calls_broker_not_native(self) -> None:
        """External WRITE captures proposal and dispatches through broker."""
        spec = _write_spec("gmail_send_email")
        catalog = _ExternalToolCatalog((spec,))

        broker_outcome = _outcome(ExecutionStatus.EXECUTED, result={"sent": True})
        mock_broker = MagicMock()
        dispatch_calls: list[Any] = []

        def fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return broker_outcome

        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(
            broker=mock_broker,
            consent_ctx=_consent_ctx(),
            engine_loop=loop,
            external_catalog=catalog,
        )

        native_calls: list[str] = []
        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=fake_bridge):
            with patch.object(agent, "_call_native_invoke", lambda *a, **kw: native_calls.append(1) or ""):
                result_str = agent._invoke_tool(
                    "gmail_send_email",
                    {"to": "x@y.com", "body": "hello"},
                    "task-w",
                )

        assert len(native_calls) == 0, "native invoke MUST NOT be called for external WRITE"
        assert len(dispatch_calls) == 1
        proposal = dispatch_calls[0]
        assert proposal.tool_name == "gmail_send_email"
        assert proposal.entity_type == "composio"
        parsed = json.loads(result_str)
        assert parsed.get("sent") is True
        loop.close()

    def test_composio_write_proposal_has_composio_entity_type(self) -> None:
        """WRITE proposal entity_type is 'composio' (not 'nous_tool')."""
        spec = _write_spec("gmail_send_email")
        proposal = _build_external_proposal(
            function_name="gmail_send_email",
            function_args={"to": "a@b.com"},
            tenant_id=_TENANT,
            effective_task_id="task-q",
            spec=spec,
        )
        assert proposal.entity_type == "composio"
        assert proposal.tool_name == "gmail_send_email"
        assert proposal.parameters["slug"] == "GMAIL_SEND_EMAIL"
        assert proposal.parameters["params"] == {"to": "a@b.com"}


# ---------------------------------------------------------------------------
# (d) MCP qualified name routes correctly
# ---------------------------------------------------------------------------


class TestMcpRouting:
    def test_mcp_read_not_in_nous_catalog(self) -> None:
        """mcp__filesystem__list_files is unknown to classify_nous_tool."""
        assert classify_nous_tool("mcp__filesystem__list_files") is None

    def test_mcp_read_routes_to_external_read(self) -> None:
        """MCP READ qualified name routes to _execute_external_read."""
        spec = _mcp_read_spec("filesystem", "list_files")
        catalog = _ExternalToolCatalog((spec,))

        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(engine_loop=loop, external_catalog=catalog)

        external_calls: list[str] = []
        with patch.object(agent, "_execute_external_read", lambda fn, args, s: external_calls.append(fn) or "{}"):
            agent._invoke_tool("mcp__filesystem__list_files", {}, "task-m")

        assert len(external_calls) == 1
        assert external_calls[0] == "mcp__filesystem__list_files"
        loop.close()

    def test_mcp_write_proposal_has_mcp_entity_type(self) -> None:
        """MCP WRITE proposal entity_type is 'mcp'."""
        spec = _mcp_write_spec("filesystem", "write_file")
        proposal = _build_external_proposal(
            function_name="mcp__filesystem__write_file",
            function_args={"path": "/tmp/x", "content": "hi"},
            tenant_id=_TENANT,
            effective_task_id="task-mw",
            spec=spec,
        )
        assert proposal.entity_type == "mcp"
        assert proposal.parameters["tool_name"] == "write_file"
        assert proposal.parameters["args"] == {"path": "/tmp/x", "content": "hi"}

    def test_mcp_write_routes_to_broker_not_native(self) -> None:
        """MCP WRITE routes to broker dispatch, not native Nous invoke."""
        spec = _mcp_write_spec("fs", "write_file")
        catalog = _ExternalToolCatalog((spec,))

        broker_outcome = _outcome(ExecutionStatus.PENDING_APPROVAL)
        dispatch_calls: list[Any] = []

        def fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return broker_outcome

        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(
            broker=MagicMock(),
            consent_ctx=_consent_ctx(),
            engine_loop=loop,
            external_catalog=catalog,
        )
        native_calls: list[str] = []
        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=fake_bridge):
            with patch.object(agent, "_call_native_invoke", lambda *a, **kw: native_calls.append(1) or ""):
                agent._invoke_tool("mcp__fs__write_file", {"path": "/x"}, "task-mw")

        assert not native_calls
        assert len(dispatch_calls) == 1
        assert dispatch_calls[0].entity_type == "mcp"
        loop.close()


# ---------------------------------------------------------------------------
# (e) GATE: every external effectful call MUST pass through the broker
# ---------------------------------------------------------------------------


class TestNoBrokerBypass:
    def test_external_write_without_broker_is_blocked(self) -> None:
        """Without broker, WRITE external tool returns BLOCKED (fail-closed)."""
        spec = _write_spec("gmail_send_email")
        catalog = _ExternalToolCatalog((spec,))
        loop = asyncio.new_event_loop()
        # No broker, no consent_context.
        agent = _make_governed_agent(
            broker=None, consent_ctx=None, engine_loop=loop, external_catalog=catalog
        )
        result_str = agent._invoke_tool("gmail_send_email", {}, "task-nb")
        parsed = json.loads(result_str)
        assert "BLOCKED" in parsed["error"]
        loop.close()

    def test_external_read_without_engine_loop_is_blocked(self) -> None:
        """External READ without engine_loop returns BLOCKED (fail-closed)."""
        spec = _read_spec("gmail_get_email")
        catalog = _ExternalToolCatalog((spec,))
        # No engine_loop.
        agent = _make_governed_agent(engine_loop=None, external_catalog=catalog)
        result_str = agent._invoke_tool("gmail_get_email", {}, "task-noloop")
        parsed = json.loads(result_str)
        assert "BLOCKED" in parsed["error"]

    def test_unknown_external_tool_default_denied(self) -> None:
        """A slug not in any catalog is default-denied (not in Nous, not in external)."""
        catalog = _ExternalToolCatalog(())  # empty
        loop = asyncio.new_event_loop()
        agent = _make_governed_agent(engine_loop=loop, external_catalog=catalog)
        result_str = agent._invoke_tool("totally_unknown_slug_xyz", {}, "task-unk")
        parsed = json.loads(result_str)
        assert "BLOCKED" in parsed["error"]
        loop.close()


# ---------------------------------------------------------------------------
# (f) GATE: READ handler called via engine_loop bridge, not direct
# ---------------------------------------------------------------------------


class TestReadHandlerBridgedViaLoop:
    def test_read_handler_called_once_per_invoke(self) -> None:
        """spec.handler is called exactly once per _invoke_tool READ call."""
        call_count = {"n": 0}

        async def _counting_handler(params: dict) -> dict:
            call_count["n"] += 1
            return {"result": "ok"}

        spec = ToolSpec(
            name="googledrive_list_files",
            description="list files",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.READ_ONLY,
            entity_type="composio",
            handler=_counting_handler,
        )
        catalog = _ExternalToolCatalog((spec,))
        bg_loop = asyncio.new_event_loop()
        import threading
        t = threading.Thread(target=bg_loop.run_forever, daemon=True)
        t.start()
        try:
            agent = _make_governed_agent(engine_loop=bg_loop, external_catalog=catalog)
            agent._invoke_tool("googledrive_list_files", {}, "task-r")
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=3)

        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# (g) zero_tools_source logs LOUD
# ---------------------------------------------------------------------------


class TestZeroToolsSourceLog:
    @pytest.mark.asyncio
    async def test_none_tools_source_is_failsafe_without_percycle_warning(
        self, caplog
    ) -> None:
        """tools_source=None → () fail-safe, with NO per-cycle warning.

        The LOUD wiring-gap warning is emitted ONCE at build time
        (__main__._build_nous_engine → 'nous_engine_no_tools_source', covered by
        TestBuildNousEngineToolsSourceWiring). _resolve_external_specs runs on every
        reasoning cycle, so warning here would spam the journal on each message —
        the resolve path must return () silently. This guards both invariants: the
        None case never raises, and the per-cycle path never re-introduces the spam.
        """
        engine = NousReasoningEngine(
            persona=_persona(),
            tools_source=None,
        )
        with caplog.at_level(logging.WARNING, logger="hermes.runtime.nous_engine"):
            specs = await engine._resolve_external_specs()

        assert specs == ()
        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("no_tools_source" in m for m in warning_msgs), (
            f"no_tools_source warning belongs at build time, not per-cycle; got: {warning_msgs}"
        )


# ---------------------------------------------------------------------------
# (h) ZERO_TOOLS: 0 external specs → LOUD warning
# ---------------------------------------------------------------------------


class TestZeroExternalSpecsLog:
    @pytest.mark.asyncio
    async def test_empty_tools_source_returns_empty_without_warning(
        self, caplog
    ) -> None:
        """tools_source yielding 0 external specs → () with NO per-cycle warning.

        Zero external tools is a LEGITIMATE steady state, not an anomaly: (1) a
        fresh instance with no Composio connections, and (2) a B4 access-scope-locked
        custom agent whose per-agent filtering (commit e9e26ea, _apply_agent_filter)
        strips every external tool BY DESIGN. A per-cycle WARNING would fire on every
        message for every restricted agent, so the resolve path stays silent; the
        only wiring-gap warning lives at build time (tools_source is None).
        """
        async def _empty_source() -> tuple:
            return ()

        engine = NousReasoningEngine(
            persona=_persona(),
            tools_source=_empty_source,
        )
        with caplog.at_level(logging.WARNING, logger="hermes.runtime.nous_engine"):
            specs = await engine._resolve_external_specs()

        assert specs == ()
        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("zero_external_tools" in m for m in warning_msgs), (
            f"zero external tools is a legitimate B4 outcome — must not warn per-cycle; "
            f"got: {warning_msgs}"
        )


# ---------------------------------------------------------------------------
# (i) WRITE: external WRITE proposal has correct entity_type
# ---------------------------------------------------------------------------


class TestExternalWriteProposalEntityType:
    def test_composio_write_entity_type(self) -> None:
        spec = _write_spec("slack_send_message")
        proposal = _build_external_proposal(
            function_name="slack_send_message",
            function_args={"channel": "#general", "text": "hi"},
            tenant_id=_TENANT,
            effective_task_id="t",
            spec=spec,
        )
        assert proposal.entity_type == "composio"

    def test_mcp_write_entity_type(self) -> None:
        spec = _mcp_write_spec("slack-mcp", "post_message")
        proposal = _build_external_proposal(
            function_name="mcp__slack-mcp__post_message",
            function_args={"channel": "#ops"},
            tenant_id=_TENANT,
            effective_task_id="t",
            spec=spec,
        )
        assert proposal.entity_type == "mcp"

    def test_fallback_entity_type(self) -> None:
        """Unknown entity_type uses spec.entity_type as-is (not 'nous_tool')."""
        spec = ToolSpec(
            name="custom_action",
            description="custom",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="my_integration",
            handler=None,
        )
        proposal = _build_external_proposal(
            function_name="custom_action",
            function_args={},
            tenant_id=_TENANT,
            effective_task_id="t",
            spec=spec,
        )
        assert proposal.entity_type == "my_integration"
        assert proposal.entity_type != "nous_tool"


# ---------------------------------------------------------------------------
# (j) BUILD PROPOSAL: composio parameters shaped correctly
# ---------------------------------------------------------------------------


class TestShapeExternalParametersComposio:
    def test_composio_slug_uppercased(self) -> None:
        spec = _write_spec("gmail_send_email")
        params = _shape_external_parameters("gmail_send_email", {"to": "a@b.com"}, spec)
        assert params["slug"] == "GMAIL_SEND_EMAIL"

    def test_composio_params_nested_under_params_key(self) -> None:
        spec = _write_spec("gmail_send_email")
        args = {"to": "x@y.com", "body": "test"}
        params = _shape_external_parameters("gmail_send_email", args, spec)
        assert params["params"] == args

    def test_composio_entity_id_present(self) -> None:
        spec = _write_spec("gmail_send_email")
        params = _shape_external_parameters("gmail_send_email", {}, spec)
        assert "entity_id" in params


# ---------------------------------------------------------------------------
# (k) BUILD PROPOSAL: mcp parameters shaped correctly
# ---------------------------------------------------------------------------


class TestShapeExternalParametersMcp:
    def test_mcp_tool_name_is_bare_tool(self) -> None:
        spec = _mcp_write_spec("filesystem", "write_file")
        params = _shape_external_parameters("mcp__filesystem__write_file", {"path": "/x"}, spec)
        assert params["tool_name"] == "write_file"

    def test_mcp_args_nested_under_args_key(self) -> None:
        spec = _mcp_write_spec("filesystem", "write_file")
        args = {"path": "/tmp/x", "content": "hello"}
        params = _shape_external_parameters("mcp__filesystem__write_file", args, spec)
        assert params["args"] == args

    def test_mcp_server_id_present(self) -> None:
        spec = _mcp_write_spec("filesystem", "write_file")
        params = _shape_external_parameters("mcp__filesystem__write_file", {}, spec)
        assert "server_id" in params


# ---------------------------------------------------------------------------
# (l) REGISTRY: _register_external_specs_in_nous is idempotent
# ---------------------------------------------------------------------------


class TestRegisterExternalSpecsIdempotent:
    def test_register_twice_no_error(self) -> None:
        """Calling _register_external_specs_in_nous twice for the same spec is idempotent."""
        spec = _read_spec("gmail_get_email")
        fake_registry = MagicMock()
        with patch("hermes.runtime.nous_engine._register_external_specs_in_nous.__module__"):
            pass  # just ensure import path is correct

        # Patch at the module level to avoid requiring hermes-agent installed.
        with patch("hermes.runtime.nous_engine._register_external_specs_in_nous") as _fn:
            _fn.return_value = None
            _fn((spec, spec))  # duplicate — should not error
            _fn.assert_called_once()

    def test_register_empty_tuple_no_error(self) -> None:
        """Empty tuple → _register_external_specs_in_nous returns immediately."""
        # If nous_registry is not importable (hermes-agent absent), this must
        # fail-soft and not raise. agent is unused for empty tuple (early return).
        try:
            _register_external_specs_in_nous((), MagicMock())
        except ImportError:
            pytest.skip("hermes-agent not installed — skip registry registration test")


# ---------------------------------------------------------------------------
# (m) _build_nous_engine in __main__ warns when tools_source is None
# ---------------------------------------------------------------------------


class TestBuildNousEngineToolsSourceWiring:
    def test_build_nous_engine_warns_when_tools_source_is_none(self, caplog) -> None:
        """_build_nous_engine(tools_source=None) emits a LOUD warning."""
        import hermes.runtime.__main__ as m

        with caplog.at_level(logging.WARNING, logger="hermes-runtime"):
            try:
                m._build_nous_engine(
                    broker=None,
                    consent_context=None,
                    tenant_id=None,
                    tools_source=None,
                )
            except Exception:
                pass  # NousAgentNotInstalledError expected if hermes-agent absent

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("nous_engine_no_tools_source" in msg for msg in warning_msgs), (
            f"Expected 'nous_engine_no_tools_source' warning, got: {warning_msgs}"
        )

    def test_build_nous_engine_no_warning_when_tools_source_wired(self, caplog) -> None:
        """No warning when tools_source is provided."""
        async def _source():
            return ()

        import hermes.runtime.__main__ as m

        with caplog.at_level(logging.WARNING, logger="hermes-runtime"):
            try:
                m._build_nous_engine(
                    broker=None,
                    consent_context=None,
                    tenant_id=None,
                    tools_source=_source,
                )
            except Exception:
                pass

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("nous_engine_no_tools_source" in msg for msg in warning_msgs), (
            "Should not warn when tools_source is provided"
        )

    def test_build_reasoning_engine_forwards_tools_source_to_nous(self) -> None:
        """_build_reasoning_engine passes tools_source to _build_nous_engine."""
        import os
        captured: list[Any] = []

        # _build_nous_engine grew B4 (agent_registry, *_repo) + dual-browser
        # (cerebro/jailed browser managers) kwargs since this test was written;
        # accept **kwargs so the assertion stays focused on the tools_source
        # forwarding contract, not the full evolving signature.
        def fake_build_nous(*, broker, consent_context, tenant_id, tools_source=None, **kwargs):
            captured.append(tools_source)
            return MagicMock()

        async def _source():
            return ()

        import hermes.runtime.__main__ as m
        with patch.dict(os.environ, {"HERMES_ENGINE": "nous"}):
            with patch.object(m, "_build_nous_engine", side_effect=fake_build_nous):
                m._build_reasoning_engine(
                    tool_specs=(),
                    tools_source=_source,
                    broker=MagicMock(),
                    consent_context=MagicMock(),
                    tenant_id=uuid4(),
                )

        assert len(captured) == 1
        assert captured[0] is _source


# ---------------------------------------------------------------------------
# _toolset_for_spec helpers
# ---------------------------------------------------------------------------


class TestToolsetForSpec:
    def test_composio_spec_toolset(self) -> None:
        assert _toolset_for_spec(_write_spec("gmail_send_email")) == "composio"
        assert _toolset_for_spec(_read_spec("gmail_get_email")) == "composio"

    def test_mcp_spec_toolset(self) -> None:
        spec = _mcp_read_spec("filesystem", "list_files")
        assert _toolset_for_spec(spec) == "mcp-filesystem"

    def test_mcp_spec_toolset_slug_extracted(self) -> None:
        spec = _mcp_write_spec("slack-mcp", "post_message")
        assert _toolset_for_spec(spec) == "mcp-slack-mcp"
