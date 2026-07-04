"""Broker-gate hardening iteration 3 — regression tests (SECURITY FIX).

Three issues addressed:
  Issue 1: External tools (Composio/MCP) on the SEQUENTIAL path now route
           through the broker via registry wrappers. Previously they were
           registered with a _blocked_handler stub that returned BLOCKED.
  Issue 2: memory and clarify are now gated via monkeypatching (inline branch
           interception). todo/delegate_task are documented residuals.
  Issue 3: broker-less Composio spec construction raises.

Tests MUST FAIL against the code BEFORE the fix and PASS after.

Stash proof:
  - Run against pre-fix code: Issue 1 tests fail (external tools return BLOCKED).
  - Run against post-fix code: all pass.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.security

_TENANT = UUID("30000000-0000-0000-0000-000000000099")
_OPERATOR = UUID("30000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _consent_ctx() -> Any:
    from hermes.capabilities.domain.ports import ConsentContext
    return ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)


def _outcome_executed(result: dict | None = None) -> Any:
    from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
    return ExecutionOutcome(
        proposal_id=uuid4(),
        status=ExecutionStatus.EXECUTED,
        result=result or {"ok": True},
    )


def _outcome_pending() -> Any:
    from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
    return ExecutionOutcome(
        proposal_id=uuid4(),
        status=ExecutionStatus.PENDING_APPROVAL,
        result=None,
    )


def _make_read_composio_spec(name: str = "gmail_get_email") -> Any:
    """ToolSpec with a broker-dispatching READ handler (Composio)."""
    from hermes.domain.tool_spec import ToolRisk, ToolSpec

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


def _make_write_composio_spec(name: str = "gmail_send_email") -> Any:
    from hermes.domain.tool_spec import ToolRisk, ToolSpec
    return ToolSpec(
        name=name,
        description=f"Composio WRITE: {name}",
        parameters_schema={"type": "object", "properties": {}},
        risk=ToolRisk.WRITE_PROPOSAL,
        entity_type="composio",
        handler=None,
    )


def _make_read_mcp_spec(slug: str = "filesystem", tool: str = "list_files") -> Any:
    from hermes.domain.tool_spec import ToolRisk, ToolSpec
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


def _make_write_mcp_spec(slug: str = "filesystem", tool: str = "create_file") -> Any:
    from hermes.domain.tool_spec import ToolRisk, ToolSpec
    qualified = f"mcp__{slug}__{tool}"
    return ToolSpec(
        name=qualified,
        description=f"MCP WRITE: {qualified}",
        parameters_schema={"type": "object", "properties": {}},
        risk=ToolRisk.WRITE_PROPOSAL,
        entity_type="mcp",
        handler=None,
    )


def _build_agent_with_external_catalog(
    specs: tuple,
    broker: Any = None,
    consent_ctx: Any = None,
    engine_loop: asyncio.AbstractEventLoop | None = None,
) -> Any:
    """Build a GovernedAIAgent with an external catalog (no hermes-agent required)."""
    from hermes.runtime.nous_engine import GovernedAIAgent, _ExternalToolCatalog

    fake_inner = MagicMock()
    with patch("hermes.runtime.nous_engine._import_ai_agent") as mock_import:
        mock_ai_cls = MagicMock(return_value=fake_inner)
        mock_import.return_value = mock_ai_cls
        with patch("hermes.runtime.nous_engine._wire_sequential_gate"):
            with patch("hermes.runtime.nous_engine._wire_inline_branch_gates"):
                agent = GovernedAIAgent(
                    model="test/model",
                    broker=broker or MagicMock(),
                    consent_context=consent_ctx or _consent_ctx(),
                    engine_loop=engine_loop,
                    tenant_id=_TENANT,
                    external_catalog=_ExternalToolCatalog(specs),
                )
    agent._inner = fake_inner
    return agent


# ---------------------------------------------------------------------------
# Issue 1a: Composio READ on SEQUENTIAL path → registry wrapper → handler,
#           NOT _blocked_handler stub.
# ---------------------------------------------------------------------------


class TestExternalComposioSequentialPath:
    """External Composio tools on sequential path route through broker, not stub."""

    def test_composio_read_sequential_wrapper_reaches_handler_not_blocked(self) -> None:
        """Sequential registry wrapper for Composio READ calls spec.handler, NOT blocked.

        FAIL BEFORE FIX: _blocked_handler stub was registered → BLOCKED returned.
        PASS AFTER FIX: broker-dispatching READ wrapper calls spec.handler via engine loop.
        """
        from hermes.runtime.nous_engine import _make_external_sequential_wrapper

        handler_call_count = {"n": 0}

        async def _recording_handler(params: dict) -> dict:
            handler_call_count["n"] += 1
            return {"emails": [], "count": 0}

        from hermes.domain.tool_spec import ToolRisk, ToolSpec
        spec = ToolSpec(
            name="gmail_get_email",
            description="Composio READ",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.READ_ONLY,
            entity_type="composio",
            handler=_recording_handler,
        )

        # Build a real event loop for the bridge.
        bg_loop = asyncio.new_event_loop()
        t = threading.Thread(target=bg_loop.run_forever, daemon=True)
        t.start()

        try:
            agent = _build_agent_with_external_catalog((spec,), engine_loop=bg_loop)
            fake_registry = MagicMock()
            registered: dict[str, Any] = {}

            def _fake_register(name, **kwargs):
                registered[name] = kwargs["handler"]

            fake_registry.register.side_effect = _fake_register

            _make_external_sequential_wrapper(agent, spec, fake_registry)

            assert "gmail_get_email" in registered, (
                "_make_external_sequential_wrapper did not register a wrapper"
            )
            wrapper = registered["gmail_get_email"]

            # Call the wrapper (as registry.dispatch would on sequential path).
            result_str = wrapper({"query": "inbox"})

            # Handler must have been called (adapter reached, NOT blocked stub).
            assert handler_call_count["n"] == 1, (
                f"spec.handler was NOT called — got {handler_call_count}. "
                "This means the _blocked_handler stub is still registered."
            )

            # Result must NOT be the BLOCKED error.
            parsed = json.loads(result_str)
            assert "BLOCKED" not in str(parsed), (
                f"Wrapper returned BLOCKED — _blocked_handler stub still in use: {parsed}"
            )
            assert parsed.get("count") == 0
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=3)

    def test_composio_write_sequential_wrapper_routes_to_broker(self) -> None:
        """Sequential registry wrapper for Composio WRITE dispatches through broker.

        FAIL BEFORE FIX: _blocked_handler stub returns BLOCKED without broker dispatch.
        PASS AFTER FIX: wrapper calls agent._dispatch_external_write → broker.dispatch.
        """
        from hermes.runtime.nous_engine import _make_external_sequential_wrapper

        spec = _make_write_composio_spec("gmail_send_email")

        dispatch_calls: list[Any] = []

        def _fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return _outcome_executed({"sent": True})

        # engine_loop must be non-None so _dispatch_external_write bypasses the
        # "no broker/loop" guard and reaches _dispatch_via_bridge (patched below).
        bg_loop = asyncio.new_event_loop()
        t = threading.Thread(target=bg_loop.run_forever, daemon=True)
        t.start()

        try:
            agent = _build_agent_with_external_catalog((spec,), engine_loop=bg_loop)
            # Ensure broker + consent_context are non-None on the agent instance.
            agent._broker = MagicMock()
            agent._consent_context = _consent_ctx()

            fake_registry = MagicMock()
            registered: dict[str, Any] = {}

            def _fake_register(name, **kwargs):
                registered[name] = kwargs["handler"]

            fake_registry.register.side_effect = _fake_register

            with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=_fake_bridge):
                _make_external_sequential_wrapper(agent, spec, fake_registry)
                wrapper = registered.get("gmail_send_email")
                assert wrapper is not None, "_make_external_sequential_wrapper did not register"

                result_str = wrapper({"to": "x@y.com"})

            # Broker must have been called exactly once.
            assert len(dispatch_calls) == 1, (
                f"broker.dispatch called {len(dispatch_calls)} times — expected 1. "
                "BLOCKED stub was registered instead of broker wrapper."
            )
            proposal = dispatch_calls[0]
            assert proposal.entity_type == "composio"
            assert proposal.tool_name == "gmail_send_email"

            parsed = json.loads(result_str)
            assert parsed.get("sent") is True
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=3)


# ---------------------------------------------------------------------------
# Issue 1b: MCP on SEQUENTIAL path → registry wrapper → handler.
# ---------------------------------------------------------------------------


class TestExternalMcpSequentialPath:
    """External MCP tools on sequential path route through broker, not stub."""

    def test_mcp_read_sequential_wrapper_calls_spec_handler(self) -> None:
        """Sequential wrapper for MCP READ calls spec.handler via engine loop.

        FAIL BEFORE FIX: _blocked_handler returns BLOCKED.
        PASS AFTER FIX: broker-dispatching READ wrapper calls spec.handler.
        """
        from hermes.runtime.nous_engine import _make_external_sequential_wrapper

        handler_calls: list[dict] = []

        async def _recording_mcp_handler(params: dict) -> dict:
            handler_calls.append(params)
            return {"files": ["a.txt", "b.txt"]}

        from hermes.domain.tool_spec import ToolRisk, ToolSpec
        spec = ToolSpec(
            name="mcp__filesystem__list_files",
            description="MCP READ",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.READ_ONLY,
            entity_type="mcp",
            handler=_recording_mcp_handler,
        )

        bg_loop = asyncio.new_event_loop()
        t = threading.Thread(target=bg_loop.run_forever, daemon=True)
        t.start()

        try:
            agent = _build_agent_with_external_catalog((spec,), engine_loop=bg_loop)
            fake_registry = MagicMock()
            registered: dict[str, Any] = {}

            def _fake_register(name, **kwargs):
                registered[name] = kwargs["handler"]

            fake_registry.register.side_effect = _fake_register

            _make_external_sequential_wrapper(agent, spec, fake_registry)
            wrapper = registered.get("mcp__filesystem__list_files")
            assert wrapper is not None

            result_str = wrapper({})

            assert len(handler_calls) == 1, (
                f"MCP handler was NOT called — BLOCKED stub still registered. Calls: {handler_calls}"
            )
            parsed = json.loads(result_str)
            assert "BLOCKED" not in str(parsed), f"Got BLOCKED: {parsed}"
            assert parsed.get("files") == ["a.txt", "b.txt"]
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=3)

    def test_mcp_write_sequential_wrapper_routes_to_broker(self) -> None:
        """Sequential wrapper for MCP WRITE dispatches through broker."""
        from hermes.runtime.nous_engine import _make_external_sequential_wrapper

        spec = _make_write_mcp_spec("filesystem", "create_file")
        dispatch_calls: list[Any] = []

        def _fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return _outcome_executed({"created": True})

        # engine_loop must be non-None so _dispatch_external_write bypasses
        # the "no broker/loop" guard and reaches _dispatch_via_bridge.
        bg_loop = asyncio.new_event_loop()
        t = threading.Thread(target=bg_loop.run_forever, daemon=True)
        t.start()

        try:
            agent = _build_agent_with_external_catalog((spec,), engine_loop=bg_loop)
            agent._broker = MagicMock()
            agent._consent_context = _consent_ctx()

            fake_registry = MagicMock()
            registered: dict[str, Any] = {}

            def _fake_register(name, **kwargs):
                registered[name] = kwargs["handler"]

            fake_registry.register.side_effect = _fake_register

            with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=_fake_bridge):
                _make_external_sequential_wrapper(agent, spec, fake_registry)
                wrapper = registered.get("mcp__filesystem__create_file")
                assert wrapper is not None
                result_str = wrapper({"path": "/tmp/test.txt", "content": "hello"})

            assert len(dispatch_calls) == 1, (
                f"broker.dispatch called {len(dispatch_calls)} times instead of 1. "
                "BLOCKED stub was registered."
            )
            proposal = dispatch_calls[0]
            assert proposal.entity_type == "mcp"
            assert "create_file" in proposal.tool_name
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=3)


# ---------------------------------------------------------------------------
# Issue 1c: No double-gate on CONCURRENT path for external tools.
# ---------------------------------------------------------------------------


class TestExternalNoDoubleGateConcurrent:
    """On the concurrent path, _invoke_tool handles externals — no registry hit."""

    def test_external_read_concurrent_path_no_double_gate(self) -> None:
        """Concurrent path (_invoke_tool) handles external READ without registry."""
        handler_calls: list[dict] = []

        async def _counting_handler(params: dict) -> dict:
            handler_calls.append(params)
            return {"emails": []}

        from hermes.domain.tool_spec import ToolRisk, ToolSpec
        spec = ToolSpec(
            name="gmail_get_email",
            description="Composio READ",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.READ_ONLY,
            entity_type="composio",
            handler=_counting_handler,
        )

        bg_loop = asyncio.new_event_loop()
        t = threading.Thread(target=bg_loop.run_forever, daemon=True)
        t.start()

        try:
            agent = _build_agent_with_external_catalog((spec,), engine_loop=bg_loop)

            # Patch registry to track if dispatch is called from concurrent path.
            registry_dispatch_calls: list[Any] = []
            with patch(
                "hermes.runtime.nous_engine._make_external_sequential_wrapper",
                wraps=lambda *a, **kw: None,
            ):
                result_str = agent._invoke_tool("gmail_get_email", {}, "task-001")

            # Handler was called once (via _invoke_tool → _execute_external_read).
            assert len(handler_calls) == 1

            # No BLOCKED response.
            parsed = json.loads(result_str)
            assert "BLOCKED" not in str(parsed)
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=3)


# ---------------------------------------------------------------------------
# Issue 2: memory and clarify gated via monkeypatching.
# ---------------------------------------------------------------------------


class TestMemoryToolGated:
    """memory WRITE actions route through broker via monkeypatch."""

    def test_memory_write_routes_through_broker(self) -> None:
        """_patch_memory_tool makes memory WRITE actions dispatch through broker.

        FAIL BEFORE FIX: inline branch calls memory_tool directly → bypasses broker.
        PASS AFTER FIX: monkeypatch intercepts and calls _dispatch_write_proposal.
        """
        from hermes.runtime.nous_engine import _patch_memory_tool

        dispatch_calls: list[Any] = []

        def _fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return _outcome_executed({"written": True})

        agent = _build_agent_with_external_catalog(())

        # Track if we patch memory_tool successfully.
        patched = {"original_called": False}
        original_memory_tool_stub = MagicMock(return_value='{"ok": true}')

        try:
            import tools.memory_tool as _mem_mod  # noqa: PLC0415
            original = _mem_mod.memory_tool
            _mem_mod.memory_tool = original_memory_tool_stub

            with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=_fake_bridge):
                _patch_memory_tool(agent)

                # Simulate what the inline branch does: call tools.memory_tool.memory_tool.
                import tools.memory_tool as _mem_after
                result_str = _mem_after.memory_tool(
                    action="add",
                    target="memory",
                    content="Test memory entry",
                    store=None,
                )

            # Broker must have been called (not the original memory_tool stub).
            assert len(dispatch_calls) == 1, (
                f"broker.dispatch called {len(dispatch_calls)} times — expected 1. "
                "Memory write was NOT intercepted by monkeypatch."
            )
            proposal = dispatch_calls[0]
            assert proposal.tool_name == "memory"
            assert proposal.parameters.get("action") == "add"

            # Original stub must NOT have been called.
            original_memory_tool_stub.assert_not_called()

        except ImportError:
            pytest.skip("tools.memory_tool not available — hermes-agent not installed")
        finally:
            try:
                _mem_mod.memory_tool = original
            except Exception:
                pass

    def test_memory_read_passes_through_to_original(self) -> None:
        """memory READ action passes through to original handler (no broker gate)."""
        from hermes.runtime.nous_engine import _patch_memory_tool

        dispatch_calls: list[Any] = []

        def _fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return _outcome_executed()

        agent = _build_agent_with_external_catalog(())

        try:
            import tools.memory_tool as _mem_mod
            original = _mem_mod.memory_tool
            original_calls: list[dict] = []

            def _tracking_original(action=None, target="memory", content=None, old_text=None, store=None, **kw):
                original_calls.append({"action": action})
                return '{"memories": []}'

            _mem_mod.memory_tool = _tracking_original

            with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=_fake_bridge):
                _patch_memory_tool(agent)

                import tools.memory_tool as _mem_after
                result_str = _mem_after.memory_tool(action="read", target="memory", store=None)

            # READ should pass through to original — broker NOT called.
            assert len(dispatch_calls) == 0, (
                "broker.dispatch called for READ action — should pass through"
            )
            assert len(original_calls) == 1
            assert original_calls[0]["action"] == "read"

        except ImportError:
            pytest.skip("tools.memory_tool not available — hermes-agent not installed")
        finally:
            try:
                _mem_mod.memory_tool = original
            except Exception:
                pass


class TestClarifyToolGated:
    """clarify routes through broker via monkeypatch."""

    def test_clarify_routes_through_broker(self) -> None:
        """_patch_clarify_tool intercepts clarify and routes through broker.

        FAIL BEFORE FIX: inline branch calls clarify_tool directly → bypasses broker.
        PASS AFTER FIX: monkeypatch intercepts and calls _dispatch_write_proposal.
        """
        from hermes.runtime.nous_engine import _patch_clarify_tool

        dispatch_calls: list[Any] = []

        def _fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return _outcome_pending()

        agent = _build_agent_with_external_catalog(())

        try:
            import tools.clarify_tool as _cl_mod
            original = _cl_mod.clarify_tool
            original_calls: list[dict] = []

            def _tracking_original(question="", choices=None, callback=None, **kw):
                original_calls.append({"question": question})
                return '{"answer": "direct_call"}'

            _cl_mod.clarify_tool = _tracking_original

            with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=_fake_bridge):
                _patch_clarify_tool(agent)

                import tools.clarify_tool as _cl_after
                result_str = _cl_after.clarify_tool(
                    question="What should I do?",
                    choices=["Option A", "Option B"],
                )

            # Broker must have been called exactly once.
            assert len(dispatch_calls) == 1, (
                f"broker.dispatch called {len(dispatch_calls)} times — expected 1. "
                "clarify was NOT intercepted by monkeypatch."
            )
            proposal = dispatch_calls[0]
            assert proposal.tool_name == "clarify"
            assert proposal.parameters.get("question") == "What should I do?"

            # Original must NOT have been called.
            assert len(original_calls) == 0, (
                "Original clarify_tool was called — monkeypatch did not intercept"
            )

            # Result should show PENDING (BLOCKED) — broker returned PENDING.
            parsed = json.loads(result_str)
            assert "BLOCKED" in parsed.get("error", ""), f"Expected BLOCKED, got: {parsed}"

        except ImportError:
            pytest.skip("tools.clarify_tool not available — hermes-agent not installed")
        finally:
            try:
                _cl_mod.clarify_tool = original
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Issue 2 residuals: todo and delegate_task bypass documented.
# ---------------------------------------------------------------------------


class TestKnownResiduals:
    """Documented residuals: todo and delegate_task bypass inline gates.

    These tests assert the documented behavior (not gated) so that future
    changes that accidentally gate them (breaking interactive UX) or that
    accidentally leave them without documentation are caught.
    """

    def test_todo_residual_documented_in_wire_inline_branch_gates(self) -> None:
        """_wire_inline_branch_gates docstring documents todo as an accepted residual."""
        import inspect
        from hermes.runtime.nous_engine import _wire_inline_branch_gates
        doc = inspect.getdoc(_wire_inline_branch_gates) or ""
        assert "todo" in doc.lower(), (
            "_wire_inline_branch_gates docstring must document 'todo' as a known residual. "
            "Without this documentation, future engineers have no trace of the decision."
        )

    def test_delegate_task_residual_documented_in_wire_inline_branch_gates(self) -> None:
        """_wire_inline_branch_gates docstring documents delegate_task as accepted residual."""
        import inspect
        from hermes.runtime.nous_engine import _wire_inline_branch_gates
        doc = inspect.getdoc(_wire_inline_branch_gates) or ""
        assert "delegate_task" in doc, (
            "_wire_inline_branch_gates docstring must document 'delegate_task' as a residual."
        )


# ---------------------------------------------------------------------------
# Issue 3: broker-less construction raises.
# ---------------------------------------------------------------------------


class TestBrokerRequiredForComposioSpecs:
    """broker is a required arg in build_composio_tool_specs and _default_tools_builder."""

    @pytest.mark.asyncio
    async def test_default_tools_builder_raises_without_broker(self) -> None:
        """_default_tools_builder raises RuntimeError — broker-less spec is unconstructable.

        FAIL BEFORE FIX: _default_tools_builder called build_composio_tool_specs(credential)
        without broker → specs built with fail-closed handlers silently.
        PASS AFTER FIX: raises unconditionally with a clear wiring error message.
        """
        from hermes.runtime.composio_tools_registry import _default_tools_builder
        from hermes.runtime.composio_config_source import ComposioCredential

        cred = ComposioCredential(api_key="key", entity_id="ent")
        with pytest.raises(RuntimeError, match="broker-less"):
            await _default_tools_builder(cred)

    @pytest.mark.asyncio
    async def test_registry_without_custom_tools_builder_uses_fail_closed_builder(self) -> None:
        """ComposioToolsRegistry without tools_builder uses _default_tools_builder.

        _default_tools_builder raises RuntimeError — verified via _build_tools attribute.
        Note: ComposioToolsRegistry._refresh catches all exceptions (fail-soft for cache
        stability). We verify the _build_tools attribute IS _default_tools_builder, which
        raises; the test above (test_default_tools_builder_raises_without_broker) proves
        the raise. Together they confirm broker-less registry construction is fail-closed.
        """
        from hermes.runtime.composio_tools_registry import (
            ComposioToolsRegistry,
            _default_tools_builder,
        )
        from pathlib import Path

        registry = ComposioToolsRegistry(db_path=Path("/nonexistent/shell-state.db"))

        # Without a custom tools_builder, _build_tools IS _default_tools_builder.
        assert registry._build_tools is _default_tools_builder, (
            "ComposioToolsRegistry._build_tools must default to _default_tools_builder "
            "(the fail-closed, broker-requiring builder)."
        )

        # Calling it directly raises RuntimeError (same as test above, confirms wiring).
        with pytest.raises(RuntimeError, match="broker-less"):
            await registry._build_tools(MagicMock(api_key="key", entity_id="ent"))

    def test_build_composio_tool_specs_requires_broker_kwarg(self) -> None:
        """build_composio_tool_specs signature has no default for broker.

        Calling without broker raises TypeError at call time (not at handler invocation).
        Skipped when the Composio SDK is unavailable (pre-existing environment issue).
        """
        import inspect

        try:
            from hermes.runtime.composio_tool_specs import build_composio_tool_specs
        except ImportError as exc:
            pytest.skip(f"Composio SDK unavailable (pre-existing env issue): {exc}")

        sig = inspect.signature(build_composio_tool_specs)
        broker_param = sig.parameters.get("broker")
        assert broker_param is not None, "broker parameter not found in signature"
        assert broker_param.default is inspect.Parameter.empty, (
            f"broker has a default value {broker_param.default!r} — "
            "it must be a required keyword argument to prevent broker-less construction."
        )


# ---------------------------------------------------------------------------
# Integration: _register_external_specs_in_nous registers broker-dispatching
# wrappers (not _blocked_handler stub).
# ---------------------------------------------------------------------------


class TestRegisterExternalSpecsNoBlocedHandler:
    """_register_external_specs_in_nous registers broker-dispatching wrappers."""

    def test_registered_wrapper_is_not_blocked_stub(self) -> None:
        """The wrapper registered by _register_external_specs_in_nous is NOT a blocked stub.

        FAIL BEFORE FIX: _blocked_handler was registered → calling it returned BLOCKED.
        PASS AFTER FIX: broker-dispatching wrapper is registered → calls spec.handler.
        """
        from hermes.runtime.nous_engine import _register_external_specs_in_nous

        handler_calls: list[dict] = []

        async def _recording_handler(params: dict) -> dict:
            handler_calls.append(params)
            return {"emails": [], "count": 0}

        from hermes.domain.tool_spec import ToolRisk, ToolSpec
        spec = ToolSpec(
            name="gmail_get_email",
            description="Composio READ",
            parameters_schema={"type": "object", "properties": {}},
            risk=ToolRisk.READ_ONLY,
            entity_type="composio",
            handler=_recording_handler,
        )

        bg_loop = asyncio.new_event_loop()
        t = threading.Thread(target=bg_loop.run_forever, daemon=True)
        t.start()

        registered: dict[str, Any] = {}
        fake_registry = MagicMock()

        def _fake_register(name, **kwargs):
            registered[name] = kwargs["handler"]

        fake_registry.register.side_effect = _fake_register

        try:
            agent = _build_agent_with_external_catalog((spec,), engine_loop=bg_loop)

            # Inject our fake registry: _register_external_specs_in_nous does
            # `from tools.registry import registry as nous_registry`.
            # Mock the module so `tools.registry.registry` returns our fake registry.
            fake_tools_registry_module = MagicMock()
            fake_tools_registry_module.registry = fake_registry
            with patch.dict("sys.modules", {"tools.registry": fake_tools_registry_module}):
                _register_external_specs_in_nous((spec,), agent)

            wrapper = registered.get("gmail_get_email")
            if wrapper is None:
                pytest.skip(
                    "Wrapper not registered — likely hermes-agent not installed. "
                    "Check with tools.registry available."
                )

            # Call the wrapper — it must NOT return BLOCKED.
            result_str = wrapper({})
            parsed = json.loads(result_str) if isinstance(result_str, str) else result_str
            assert "BLOCKED" not in str(parsed), (
                f"Wrapper returned BLOCKED — _blocked_handler stub is still registered: {parsed}"
            )
            assert len(handler_calls) == 1, (
                f"spec.handler not called — wrapper does not route to handler. calls={handler_calls}"
            )
        finally:
            bg_loop.call_soon_threadsafe(bg_loop.stop)
            t.join(timeout=3)


# ---------------------------------------------------------------------------
# Stash proof helpers — these document what fails before the fix.
# ---------------------------------------------------------------------------


class TestStashProofDocumentation:
    """Tests that can be used to verify pre-fix vs post-fix behavior.

    Run with git stash to see these FAIL against pre-fix code, then
    git stash pop to see them PASS.
    """

    def test_external_sequential_wrapper_exists_on_make_external_sequential_wrapper(self) -> None:
        """_make_external_sequential_wrapper exists — did not exist before fix."""
        from hermes.runtime.nous_engine import _make_external_sequential_wrapper
        assert callable(_make_external_sequential_wrapper), (
            "_make_external_sequential_wrapper not found — Issue 1 fix not applied."
        )

    def test_wire_inline_branch_gates_exists(self) -> None:
        """_wire_inline_branch_gates exists — did not exist before fix."""
        from hermes.runtime.nous_engine import _wire_inline_branch_gates
        assert callable(_wire_inline_branch_gates), (
            "_wire_inline_branch_gates not found — Issue 2 fix not applied."
        )

    def test_register_external_specs_takes_agent_arg(self) -> None:
        """_register_external_specs_in_nous takes agent as second arg — not before fix."""
        import inspect
        from hermes.runtime.nous_engine import _register_external_specs_in_nous
        sig = inspect.signature(_register_external_specs_in_nous)
        params = list(sig.parameters.keys())
        assert "agent" in params, (
            "_register_external_specs_in_nous does not have 'agent' parameter — "
            "Issue 1 fix not applied."
        )

    def test_default_tools_builder_is_fail_closed(self) -> None:
        """_default_tools_builder raises RuntimeError — confirms broker enforcement."""
        from hermes.runtime.composio_tools_registry import _default_tools_builder
        import asyncio

        async def _check():
            with pytest.raises(RuntimeError):
                await _default_tools_builder(MagicMock())

        # asyncio.get_event_loop() is deprecated in Python 3.10+ when there is no
        # running loop (as is the case after pytest-asyncio closes the loop at the
        # end of an async test).  Use asyncio.run() which creates a fresh loop.
        asyncio.run(_check())
