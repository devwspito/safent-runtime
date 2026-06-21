"""Tests for spec 014 increment 3: capability ToolSpec bridge.

Verifies (deterministically — no real LLM, no real broker, no real UNO):

  (a) COUNT: build_capability_tool_specs returns > 0 specs including lo_write_text.
  (b) DESKTOP_APP READ: lo_open_document is READ_ONLY with a handler.
  (c) DESKTOP_APP WRITE: lo_write_text is WRITE_PROPOSAL with handler=None.
  (d) OP INJECTION (READ): lo_open_document handler injects op="open_document"
      into the proposal going to the broker.
  (e) OP INJECTION (WRITE): _shape_external_parameters injects op="write_text"
      for lo_write_text (os_surface entity type) so the adapter receives it.
  (f) BROKER GATE (READ): calling the lo_open_document handler reaches
      broker.dispatch exactly once — no direct adapter call.
  (g) BROKER GATE (WRITE): dispatching lo_write_text as an external WRITE from
      GovernedAIAgent routes to broker.dispatch (not native invoke) and
      returns PENDING_APPROVAL for HIGH risk without HITL token.
  (h) NO BYPASS: None of the capability spec names bypasses the broker.
  (i) EXCLUSION: OS_NATIVE_SKILLS are NOT in the capability specs output.
  (j) EXCLUSION: Nous-native names (write_file, memory, etc.) are NOT included.
  (k) TOOLSET: os_surface specs get "os_surface" toolset in _toolset_for_spec.
  (l) tools_source includes capability specs: count > 6 after wiring.
  (m) _resolve_external_specs accepts os_surface specs (name not in Nous catalog).
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
from hermes.runtime.capability_tool_specs import (
    _DESKTOP_APP_OP_MAP,
    _OS_NATIVE_SKILL_NAMES,
    _NOUS_NATIVE_NAMES,
    _NOUS_NATIVE_DUPLICATES,
    _TOOL_SCHEMAS,
    build_capability_tool_specs,
)
from hermes.runtime.nous_engine import (
    _ExternalToolCatalog,
    _toolset_for_spec,
    _shape_external_parameters,
    NousReasoningEngine,
)

pytestmark = pytest.mark.unit

_TENANT = UUID("20000000-0000-0000-0000-000000000001")
_OPERATOR = UUID("20000000-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consent_ctx() -> Any:
    from hermes.capabilities.domain.ports import ConsentContext
    return ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)


def _persona():
    from hermes.prompts.persona import PersonaSpec
    return PersonaSpec(
        name="Lumen",
        role="test-role",
        language="es",
        register="formal",
        primary_mission="testing",
    )


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


def _mock_broker(
    dispatch_status: ExecutionStatus = ExecutionStatus.EXECUTED,
    dispatch_result: dict | None = None,
) -> Any:
    """Return an AsyncMock broker whose dispatch returns a predictable outcome."""
    broker = MagicMock()
    broker.dispatch = AsyncMock(
        return_value=_outcome(dispatch_status, result=dispatch_result or {})
    )
    return broker


def _build_specs(
    broker: Any = None,
    consent_context: Any = None,
) -> tuple[ToolSpec, ...]:
    specs, _ref = build_capability_tool_specs(
        broker=broker or _mock_broker(),
        consent_context=consent_context or _consent_ctx(),
    )
    return specs


# ---------------------------------------------------------------------------
# (a) COUNT: > 0 specs, includes lo_write_text
# ---------------------------------------------------------------------------


class TestBuildCapabilityToolSpecsCount:
    def test_returns_nonempty_tuple(self) -> None:
        specs = _build_specs()
        assert len(specs) > 0

    def test_includes_lo_write_text(self) -> None:
        specs = _build_specs()
        names = {s.name for s in specs}
        assert "lo_write_text" in names, (
            f"lo_write_text missing from capability specs. Got: {sorted(names)}"
        )

    def test_includes_lo_open_document(self) -> None:
        specs = _build_specs()
        names = {s.name for s in specs}
        assert "lo_open_document" in names

    def test_includes_lo_save_document(self) -> None:
        specs = _build_specs()
        names = {s.name for s in specs}
        assert "lo_save_document" in names

    def test_run_command_removed_as_nous_native_duplicate(self) -> None:
        """run_command is superseded by Nous-native 'terminal' — must NOT appear in LLM schema."""
        specs = _build_specs()
        names = {s.name for s in specs}
        assert "run_command" not in names, (
            "run_command has a Nous-native equivalent ('terminal') and must be excluded "
            "from capability specs to avoid LLM ambiguity and wasted tokens."
        )
        assert "run_command" in _NOUS_NATIVE_DUPLICATES

    def test_includes_navigate_app(self) -> None:
        specs = _build_specs()
        names = {s.name for s in specs}
        assert "navigate_app" in names

    def test_excludes_gui_control_customs_replaced_by_native_computer_use(self) -> None:
        # click_app_element / type_in_app son control GUI de elementos →
        # reemplazados por el toolset NATIVO computer_use (lumen-cua-driver).
        # NO deben registrarse al LLM (Hermes nativo sin más).
        specs = _build_specs()
        names = {s.name for s in specs}
        assert "click_app_element" not in names
        assert "type_in_app" not in names


# ---------------------------------------------------------------------------
# (b) DESKTOP_APP READ: lo_open_document is READ_ONLY with a handler
# ---------------------------------------------------------------------------


class TestDesktopAppReadSpec:
    def test_lo_open_document_is_read_only(self) -> None:
        specs = _build_specs()
        spec = next((s for s in specs if s.name == "lo_open_document"), None)
        assert spec is not None
        assert spec.risk == ToolRisk.READ_ONLY

    def test_lo_open_document_has_handler(self) -> None:
        specs = _build_specs()
        spec = next((s for s in specs if s.name == "lo_open_document"), None)
        assert spec is not None
        assert spec.handler is not None

    def test_lo_open_document_entity_type_os_surface(self) -> None:
        specs = _build_specs()
        spec = next((s for s in specs if s.name == "lo_open_document"), None)
        assert spec is not None
        assert spec.entity_type == "os_surface"


# ---------------------------------------------------------------------------
# (c) DESKTOP_APP WRITE: lo_write_text is WRITE_PROPOSAL with handler=None
# ---------------------------------------------------------------------------


class TestDesktopAppWriteSpec:
    def test_lo_write_text_is_write_proposal(self) -> None:
        specs = _build_specs()
        spec = next((s for s in specs if s.name == "lo_write_text"), None)
        assert spec is not None
        assert spec.risk == ToolRisk.WRITE_PROPOSAL

    def test_lo_write_text_has_no_handler(self) -> None:
        specs = _build_specs()
        spec = next((s for s in specs if s.name == "lo_write_text"), None)
        assert spec is not None
        assert spec.handler is None

    def test_lo_save_document_is_write_proposal(self) -> None:
        specs = _build_specs()
        spec = next((s for s in specs if s.name == "lo_save_document"), None)
        assert spec is not None
        assert spec.risk == ToolRisk.WRITE_PROPOSAL


# ---------------------------------------------------------------------------
# (d) OP INJECTION (READ): lo_open_document handler injects op into proposal
# ---------------------------------------------------------------------------


class TestDesktopAppOpInjectionRead:
    @pytest.mark.asyncio
    async def test_lo_open_document_handler_injects_op(self) -> None:
        """The lo_open_document READ handler injects op='open_document' into the broker proposal."""
        dispatched_proposals: list[Any] = []

        broker = MagicMock()

        async def _capturing_dispatch(proposal, consent_ctx, **kwargs):
            dispatched_proposals.append(proposal)
            return _outcome(ExecutionStatus.EXECUTED, result={"opened": True})

        broker.dispatch = _capturing_dispatch

        specs, _ref = build_capability_tool_specs(
            broker=broker,
            consent_context=_consent_ctx(),
        )
        spec = next((s for s in specs if s.name == "lo_open_document"), None)
        assert spec is not None
        assert spec.handler is not None

        result = await spec.handler({"document_path": "/tmp/test.odt"})

        assert len(dispatched_proposals) == 1
        proposal = dispatched_proposals[0]
        assert proposal.tool_name == "lo_open_document"
        assert proposal.parameters.get("op") == "open_document", (
            f"Expected op='open_document' in parameters, got: {proposal.parameters}"
        )
        assert proposal.parameters.get("document_path") == "/tmp/test.odt"
        assert result.get("opened") is True

    @pytest.mark.asyncio
    async def test_lo_open_document_handler_routes_through_broker_not_direct(self) -> None:
        """The handler never calls the UNO adapter directly — ONLY broker.dispatch."""
        broker = _mock_broker(
            dispatch_status=ExecutionStatus.EXECUTED,
            dispatch_result={"opened": True},
        )
        specs, _ref = build_capability_tool_specs(broker=broker, consent_context=_consent_ctx())
        spec = next((s for s in specs if s.name == "lo_open_document"), None)
        assert spec is not None

        await spec.handler({"document_path": "/tmp/doc.odt"})

        broker.dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_lo_open_document_handler_blocked_returns_error_dict(self) -> None:
        """If broker returns REJECTED_BY_POLICY, handler returns error dict (no exception)."""
        broker = _mock_broker(dispatch_status=ExecutionStatus.REJECTED_BY_POLICY, dispatch_result={})
        broker.dispatch = AsyncMock(
            return_value=_outcome(ExecutionStatus.REJECTED_BY_POLICY, error="blocked")
        )
        specs, _ref = build_capability_tool_specs(broker=broker, consent_context=_consent_ctx())
        spec = next((s for s in specs if s.name == "lo_open_document"), None)
        assert spec is not None

        result = await spec.handler({"document_path": "/tmp/test.odt"})

        assert "error" in result
        assert "rejected_by_policy" in result["error"].lower() or "blocked" in result["error"].lower()


# ---------------------------------------------------------------------------
# (e) OP INJECTION (WRITE): _shape_external_parameters for lo_write_text
# ---------------------------------------------------------------------------


class TestOpInjectionWrite:
    def test_shape_parameters_lo_write_text_injects_op(self) -> None:
        """_shape_external_parameters for os_surface lo_write_text adds op='write_text'."""
        spec = ToolSpec(
            name="lo_write_text",
            description="write text",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="os_surface",
            handler=None,
        )
        args = {"document_path": "/tmp/doc.odt", "text": "Hello from Hermes"}

        params = _shape_external_parameters("lo_write_text", args, spec)

        assert params.get("op") == "write_text", (
            f"Expected op='write_text', got: {params}"
        )
        assert params.get("document_path") == "/tmp/doc.odt"
        assert params.get("text") == "Hello from Hermes"

    def test_shape_parameters_lo_save_document_injects_op(self) -> None:
        """lo_save_document → op='save_document'."""
        spec = ToolSpec(
            name="lo_save_document",
            description="save",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="os_surface",
            handler=None,
        )
        params = _shape_external_parameters(
            "lo_save_document",
            {"document_path": "/tmp/doc.odt"},
            spec,
        )
        assert params.get("op") == "save_document"

    def test_shape_parameters_non_desktop_app_no_op(self) -> None:
        """OS surface tools that are NOT DESKTOP_APP (e.g. run_command) do NOT get op injected."""
        spec = ToolSpec(
            name="run_command",
            description="run",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="os_surface",
            handler=None,
        )
        params = _shape_external_parameters(
            "run_command",
            {"argv": ["ls", "/tmp"]},
            spec,
        )
        assert "op" not in params
        assert params.get("argv") == ["ls", "/tmp"]


# ---------------------------------------------------------------------------
# (f) BROKER GATE (READ): lo_open_document handler reaches broker.dispatch once
# ---------------------------------------------------------------------------


class TestBrokerGateRead:
    @pytest.mark.asyncio
    async def test_lo_open_document_read_reaches_broker_exactly_once(self) -> None:
        dispatch_count = {"n": 0}

        broker = MagicMock()

        async def _dispatch(proposal, ctx, **kwargs):
            dispatch_count["n"] += 1
            return _outcome(ExecutionStatus.EXECUTED)

        broker.dispatch = _dispatch

        specs, _ref = build_capability_tool_specs(broker=broker, consent_context=_consent_ctx())
        spec = next(s for s in specs if s.name == "lo_open_document")
        await spec.handler({"document_path": "/tmp/x.odt"})

        assert dispatch_count["n"] == 1, (
            f"broker.dispatch called {dispatch_count['n']} times — must be exactly 1"
        )


# ---------------------------------------------------------------------------
# (g) BROKER GATE (WRITE): GovernedAIAgent routes lo_write_text to broker
# ---------------------------------------------------------------------------


class TestBrokerGateWrite:
    def test_lo_write_text_as_external_write_routes_to_broker(self) -> None:
        """GovernedAIAgent._invoke_tool for os_surface WRITE routes to broker.dispatch."""
        from hermes.runtime.nous_engine import GovernedAIAgent

        spec = ToolSpec(
            name="lo_write_text",
            description="write text",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="os_surface",
            handler=None,
        )
        catalog = _ExternalToolCatalog((spec,))

        dispatch_calls: list[Any] = []
        pending_outcome = _outcome(ExecutionStatus.PENDING_APPROVAL)

        def fake_bridge(*, proposal, broker, consent_context, engine_loop):
            dispatch_calls.append(proposal)
            return pending_outcome

        loop = asyncio.new_event_loop()
        fake_inner = MagicMock()
        with patch("hermes.runtime.nous_engine._import_ai_agent") as mock_import:
            mock_ai_cls = MagicMock(return_value=fake_inner)
            mock_import.return_value = mock_ai_cls
            agent = GovernedAIAgent(
                model="test/model",
                broker=MagicMock(),
                consent_context=_consent_ctx(),
                engine_loop=loop,
                tenant_id=_TENANT,
                external_catalog=catalog,
            )
        agent._inner = fake_inner

        native_calls: list[str] = []
        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=fake_bridge):
            with patch.object(agent, "_call_native_invoke", lambda *a, **kw: native_calls.append(1) or ""):
                result_str = agent._invoke_tool(
                    "lo_write_text",
                    {"document_path": "/tmp/test.odt", "text": "Hola desde Hermes"},
                    "task-libreoffice",
                )

        assert not native_calls, "native Nous invoke MUST NOT be called for os_surface WRITE"
        assert len(dispatch_calls) == 1
        proposal = dispatch_calls[0]
        assert proposal.tool_name == "lo_write_text"
        assert proposal.entity_type == "os_surface"

        # op must be injected into the proposal parameters
        assert proposal.parameters.get("op") == "write_text", (
            f"Expected op='write_text' in parameters, got: {proposal.parameters}"
        )
        assert proposal.parameters.get("text") == "Hola desde Hermes"

        # Result from PENDING_APPROVAL is "BLOCKED: pendiente de aprobación HITL..." —
        # the exact wording is locale-dependent; just verify it's a BLOCKED/error response
        # (not EXECUTED), which means the agent did not proceed to the adapter.
        parsed = json.loads(result_str)
        result_str_lower = str(parsed).lower()
        assert "blocked" in result_str_lower or "pending" in result_str_lower or "hitl" in result_str_lower, (
            f"Expected BLOCKED/PENDING/HITL in result, got: {parsed}"
        )

        loop.close()

    def test_lo_write_text_pending_approval_without_hitl(self) -> None:
        """lo_write_text is HIGH risk → proposal is blocked for HITL (broker contract)."""
        from hermes.runtime.nous_engine import GovernedAIAgent

        spec = ToolSpec(
            name="lo_write_text",
            description="write text",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="os_surface",
            handler=None,
        )
        catalog = _ExternalToolCatalog((spec,))

        loop = asyncio.new_event_loop()
        pending = _outcome(ExecutionStatus.PENDING_APPROVAL)

        def fake_bridge(*, proposal, broker, consent_context, engine_loop):
            return pending

        fake_inner = MagicMock()
        with patch("hermes.runtime.nous_engine._import_ai_agent") as mock_import:
            mock_ai_cls = MagicMock(return_value=fake_inner)
            mock_import.return_value = mock_ai_cls
            agent = GovernedAIAgent(
                model="test/model",
                broker=MagicMock(),
                consent_context=_consent_ctx(),
                engine_loop=loop,
                tenant_id=_TENANT,
                external_catalog=catalog,
            )
        agent._inner = fake_inner

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=fake_bridge):
            result_str = agent._invoke_tool(
                "lo_write_text",
                {"document_path": "/tmp/test.odt", "text": "hello"},
                "task-hitl",
            )

        # PENDING_APPROVAL maps to a "BLOCKED" response string so the LLM knows
        # the action is pending human approval.
        result_lower = result_str.lower()
        assert "blocked" in result_lower or "pending" in result_lower or "hitl" in result_lower, (
            f"Expected BLOCKED/PENDING/HITL indicator in result, got: {result_str}"
        )
        loop.close()


# ---------------------------------------------------------------------------
# (h) NO BYPASS: capability spec names resolve to registered tools in the registry
# ---------------------------------------------------------------------------


class TestNoBrokerBypass:
    def test_all_capability_specs_have_registered_binding(self) -> None:
        """Every capability spec name resolves to a binding in CapabilityRegistry (no orphan tools)."""
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        registry = CapabilityRegistry()

        specs = _build_specs()
        unregistered = [
            s.name for s in specs
            if registry.resolve(s.name) is None
        ]
        assert not unregistered, (
            f"Capability specs without registry binding (broker will fail-close): "
            f"{unregistered}"
        )

    def test_read_spec_handler_calls_broker_not_adapter(self) -> None:
        """READ handler for lo_open_document does NOT call UNO adapter directly."""
        # Patch the UNO adapter to verify it is never called.
        with patch(
            "hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter.LibreOfficeUnoSurfaceAdapter.replay",
        ) as mock_replay:
            broker = _mock_broker(
                dispatch_status=ExecutionStatus.EXECUTED,
                dispatch_result={"opened": True},
            )
            specs, _ref = build_capability_tool_specs(broker=broker, consent_context=_consent_ctx())
            spec = next(s for s in specs if s.name == "lo_open_document")

            asyncio.run(spec.handler({"document_path": "/tmp/x.odt"}))

        mock_replay.assert_not_called()


# ---------------------------------------------------------------------------
# (i) EXCLUSION: OS_NATIVE_SKILLS are NOT in capability specs
# ---------------------------------------------------------------------------


class TestExclusions:
    def test_os_native_skills_excluded(self) -> None:
        """OS_NATIVE_SKILLS (screenshot, screen_record, etc.) must not appear in capability specs."""
        specs = _build_specs()
        names = {s.name for s in specs}
        for native in _OS_NATIVE_SKILL_NAMES:
            assert native not in names, (
                f"OS_NATIVE_SKILL {native!r} appeared in capability specs — "
                "it would create a duplicate ToolSpec with conflicting handlers."
            )

    def test_nous_native_names_excluded(self) -> None:
        """Nous-native tool names (write_file, memory, etc.) must not appear in capability specs."""
        specs = _build_specs()
        names = {s.name for s in specs}
        for nous_name in _NOUS_NATIVE_NAMES:
            assert nous_name not in names, (
                f"Nous-native tool {nous_name!r} appeared in capability specs — "
                "it would collide with Nous's own dispatch logic."
            )

    def test_nous_native_duplicates_excluded(self) -> None:
        """Tools superseded by Nous-native equivalents must not appear in capability specs."""
        specs = _build_specs()
        names = {s.name for s in specs}
        for dup_name in _NOUS_NATIVE_DUPLICATES:
            assert dup_name not in names, (
                f"Duplicate tool {dup_name!r} appeared in capability specs — "
                "it has a Nous-native equivalent and causes LLM ambiguity."
            )


# ---------------------------------------------------------------------------
# (j) Specifically verify write_file and memory are NOT included
# ---------------------------------------------------------------------------


class TestSpecificExclusions:
    def test_write_file_not_in_capability_specs(self) -> None:
        """write_file is in _NOUS_TOOL_RISK — must be excluded."""
        specs = _build_specs()
        names = {s.name for s in specs}
        assert "write_file" not in names

    def test_memory_not_in_capability_specs(self) -> None:
        """memory is Nous-native — must be excluded."""
        specs = _build_specs()
        names = {s.name for s in specs}
        assert "memory" not in names

    def test_screenshot_not_in_capability_specs(self) -> None:
        """screenshot is an OS_NATIVE_SKILL — excluded to avoid ToolSpec duplication."""
        specs = _build_specs()
        names = {s.name for s in specs}
        assert "screenshot" not in names


# ---------------------------------------------------------------------------
# (k) TOOLSET: os_surface entity type gets "os_surface" toolset
# ---------------------------------------------------------------------------


class TestToolsetForOsSurface:
    def test_os_surface_spec_gets_os_surface_toolset(self) -> None:
        """_toolset_for_spec returns 'os_surface' for entity_type='os_surface'."""
        spec = ToolSpec(
            name="lo_write_text",
            description="write",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.WRITE_PROPOSAL,
            entity_type="os_surface",
            handler=None,
        )
        assert _toolset_for_spec(spec) == "os_surface"

    def test_lo_open_document_spec_from_build_has_os_surface_toolset(self) -> None:
        specs = _build_specs()
        spec = next(s for s in specs if s.name == "lo_open_document")
        assert _toolset_for_spec(spec) == "os_surface"

    def test_composio_spec_still_gets_composio_toolset(self) -> None:
        """Composio specs are unaffected by the os_surface addition."""
        spec = ToolSpec(
            name="gmail_get_email",
            description="get email",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.READ_ONLY,
            entity_type="composio",
            handler=AsyncMock(return_value={}),
        )
        assert _toolset_for_spec(spec) == "composio"


# ---------------------------------------------------------------------------
# (l) tools_source includes capability specs: count > 6
# ---------------------------------------------------------------------------


class TestToolsSourceCount:
    @pytest.mark.asyncio
    async def test_resolve_external_specs_includes_lo_write_text(self) -> None:
        """After wiring, _resolve_external_specs returns lo_write_text in the catalog."""
        # Build a fake tools_source that returns capability specs + 6 os_native
        broker = _mock_broker()
        consent = _consent_ctx()
        cap_specs, _ref = build_capability_tool_specs(broker=broker, consent_context=consent)

        # Simulate what _tools_source returns: native_os (6) + capability specs
        all_specs = cap_specs  # just test with capability specs (exclude native OS for isolation)

        async def _source() -> tuple:
            return all_specs

        engine = NousReasoningEngine(
            persona=_persona(),
            tools_source=_source,
        )
        resolved = await engine._resolve_external_specs()

        # Must include lo_write_text and lo_open_document.
        # run_command is intentionally absent (superseded by Nous-native 'terminal').
        names = {s.name for s in resolved}
        assert "lo_write_text" in names, (
            f"lo_write_text not in resolved external specs. Got: {sorted(names)}"
        )
        assert "lo_open_document" in names
        assert "run_command" not in names, (
            "run_command must not appear — it is superseded by Nous-native 'terminal'"
        )

    @pytest.mark.asyncio
    async def test_capability_specs_pass_nous_catalog_filter(self) -> None:
        """All capability spec names pass classify_nous_tool(name) is None filter."""
        from hermes.runtime.nous_tool_risk_map import classify_nous_tool

        broker = _mock_broker()
        specs, _ref = build_capability_tool_specs(broker=broker, consent_context=_consent_ctx())
        filtered_out = [
            s.name for s in specs
            if classify_nous_tool(s.name) is not None
        ]
        assert not filtered_out, (
            f"These capability spec names are in the Nous catalog and would be "
            f"filtered out by _resolve_external_specs: {filtered_out}. "
            "Add them to _NOUS_NATIVE_NAMES in capability_tool_specs.py."
        )

    @pytest.mark.asyncio
    async def test_total_external_count_exceeds_six(self) -> None:
        """After including capability specs, the external tool count is > 6 (not just Composio)."""
        broker = _mock_broker()
        consent = _consent_ctx()
        cap_specs, _ref = build_capability_tool_specs(broker=broker, consent_context=consent)

        async def _source() -> tuple:
            return cap_specs

        engine = NousReasoningEngine(persona=_persona(), tools_source=_source)
        resolved = await engine._resolve_external_specs()

        assert len(resolved) > 6, (
            f"Expected more than 6 external tools (capability specs), got {len(resolved)}. "
            "The LLM can only see Composio tools — native capabilities are missing."
        )


# ---------------------------------------------------------------------------
# (m) _resolve_external_specs accepts os_surface specs (name not in Nous catalog)
# ---------------------------------------------------------------------------


class TestResolveExternalSpecsAcceptsOsSurface:
    @pytest.mark.asyncio
    async def test_lo_write_text_not_filtered_by_nous_catalog(self) -> None:
        """lo_write_text is not in the Nous native catalog → not filtered out."""
        from hermes.runtime.nous_tool_risk_map import classify_nous_tool

        assert classify_nous_tool("lo_write_text") is None, (
            "lo_write_text must NOT be in the Nous native catalog — "
            "it would be filtered out by _resolve_external_specs."
        )

    @pytest.mark.asyncio
    async def test_navigate_app_not_filtered_by_nous_catalog(self) -> None:
        from hermes.runtime.nous_tool_risk_map import classify_nous_tool
        assert classify_nous_tool("navigate_app") is None

    @pytest.mark.asyncio
    async def test_run_command_not_filtered_by_nous_catalog(self) -> None:
        from hermes.runtime.nous_tool_risk_map import classify_nous_tool
        assert classify_nous_tool("run_command") is None


# ---------------------------------------------------------------------------
# DESKTOP_APP_OP_MAP invariant
# ---------------------------------------------------------------------------


class TestDesktopAppOpMap:
    def test_lo_open_document_op_is_open_document(self) -> None:
        assert _DESKTOP_APP_OP_MAP["lo_open_document"] == "open_document"

    def test_lo_write_text_op_is_write_text(self) -> None:
        assert _DESKTOP_APP_OP_MAP["lo_write_text"] == "write_text"

    def test_lo_save_document_op_is_save_document(self) -> None:
        assert _DESKTOP_APP_OP_MAP["lo_save_document"] == "save_document"

    def test_all_desktop_app_op_map_names_have_schema(self) -> None:
        """Every DESKTOP_APP_OP_MAP name must have a schema so it appears in LLM schema."""
        for name in _DESKTOP_APP_OP_MAP:
            assert name in _TOOL_SCHEMAS, (
                f"{name!r} in DESKTOP_APP_OP_MAP but missing from _TOOL_SCHEMAS. "
                "The LLM will not see this tool."
            )
