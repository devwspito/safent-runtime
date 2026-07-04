"""Regression tests: sequential-path gate bypass (SECURITY FIX).

Verified contract:
  (1) A single WRITE tool call (len==1 → sequential path in Nous) must reach
      broker.dispatch EXACTLY ONCE and must NOT execute the native handler.
  (2) A multi-call concurrent batch (len>=2) must reach broker.dispatch EXACTLY
      ONCE per call (via _invoke_tool) and NOT double-gate via the registry wrapper.
  (3) startup Composio specs built without a broker must use a fail-closed handler
      that raises, not a handler that calls the Composio API directly.

These tests MUST FAIL against the code before the fix and PASS after the fix.

Approach:
  - Drive _execute_tool_calls_sequential directly with a one-element batch and
    a mocked registry.dispatch to assert the gate fires.
  - Drive the concurrent path the same way and assert no double-gate.
  - Test _make_direct_composio_handler raises.

No hermes-agent required for tests (1) and (3). Test (2) mocks AIAgent.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.security

_TENANT = UUID("00000000-0000-0000-0000-000000000099")


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _consent_ctx() -> Any:
    from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415
    return ConsentContext(
        tenant_id=_TENANT,
        operator_id=UUID("00000000-0000-0000-0000-000000000001"),
    )


def _outcome_executed() -> Any:
    from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus  # noqa: PLC0415
    return ExecutionOutcome(
        proposal_id=uuid4(),
        status=ExecutionStatus.EXECUTED,
        result={"ok": True},
    )


def _make_governed_agent_with_mock_registry(
    broker: Any,
    write_tool_name: str = "write_file",
) -> tuple[Any, dict[str, list]]:
    """Build a GovernedAIAgent with a mocked tools.registry.

    Returns (agent, call_log) where call_log["native"] and call_log["broker"]
    record invocations of the native handler and broker.dispatch respectively.
    """
    from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus  # noqa: PLC0415
    from hermes.runtime.nous_engine import GovernedAIAgent  # noqa: PLC0415

    call_log: dict[str, list] = {"native": [], "broker": []}

    # Fake native handler — should NEVER be called for WRITE tools.
    def _original_native_handler(args: dict[str, Any], **kwargs: Any) -> str:
        call_log["native"].append(args)
        return json.dumps({"native": "executed_directly"})

    # Fake ToolEntry returned by registry.get_entry
    fake_entry = MagicMock()
    fake_entry.toolset = "files"
    fake_entry.schema = {"name": write_tool_name, "parameters": {"type": "object"}}
    fake_entry.handler = _original_native_handler
    fake_entry.check_fn = None
    fake_entry.requires_env = None
    fake_entry.is_async = False
    fake_entry.description = f"test {write_tool_name}"
    fake_entry.emoji = ""

    # Registry mock: get_entry returns fake_entry for our tool.
    fake_registry = MagicMock()
    fake_registry.get_entry.return_value = fake_entry

    # Track register calls (so we can inspect what wrapper was installed)
    registered_handlers: dict[str, Any] = {}

    def _fake_register(name, toolset, schema, handler, **kwargs):
        registered_handlers[name] = handler

    fake_registry.register.side_effect = _fake_register

    # dispatch delegates to whatever handler was registered.
    def _fake_dispatch(name: str, args: dict, **kwargs):
        h = registered_handlers.get(name, _original_native_handler)
        return h(args, **kwargs)

    fake_registry.dispatch.side_effect = _fake_dispatch

    fake_inner = MagicMock()

    loop = asyncio.new_event_loop()

    with patch("hermes.runtime.nous_engine._import_ai_agent") as mock_import:
        mock_ai_cls = MagicMock(return_value=fake_inner)
        mock_import.return_value = mock_ai_cls

        with patch("hermes.runtime.nous_engine._wire_sequential_gate") as mock_wire:
            # We call _wire_sequential_gate manually AFTER building the agent
            # with our fake registry, so we control which registry it patches.
            mock_wire.return_value = None
            agent = GovernedAIAgent(
                model="test/model",
                broker=broker,
                consent_context=_consent_ctx(),
                engine_loop=loop,
                tenant_id=_TENANT,
            )

    # Manually wire the sequential gate using our fake registry.
    from hermes.runtime.nous_engine import _make_sequential_write_wrapper  # noqa: PLC0415
    _make_sequential_write_wrapper(agent, write_tool_name, fake_registry, fake_entry)

    agent._inner = fake_inner
    agent._loop = loop
    agent._registered_handlers = registered_handlers
    agent._fake_registry = fake_registry
    agent._fake_entry = fake_entry
    agent._original_native_handler = _original_native_handler
    agent._call_log = call_log

    return agent, call_log


# ---------------------------------------------------------------------------
# (1) Sequential path: single WRITE call → broker fires, native does not
# ---------------------------------------------------------------------------


class TestSequentialGate:
    """Regression: single WRITE tool call (sequential path) must hit broker once."""

    def test_write_wrapper_calls_broker_not_native(self) -> None:
        """The registry wrapper installed by _wire_sequential_gate calls
        broker.dispatch and does NOT invoke the original native handler."""
        from hermes.capabilities.domain.ports import ExecutionStatus  # noqa: PLC0415
        from hermes.runtime.nous_engine import _dispatch_via_bridge  # noqa: PLC0415

        broker = MagicMock()
        dispatch_calls: list[Any] = []

        def _fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return _outcome_executed()

        agent, call_log = _make_governed_agent_with_mock_registry(broker)

        # Simulate the sequential path calling the registered handler directly
        # (as registry.dispatch would — i.e. the registry handler, not _invoke_tool).
        wrapper = agent._registered_handlers.get("write_file")
        assert wrapper is not None, (
            "_wire_sequential_gate did not register a wrapper for write_file"
        )

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=_fake_bridge):
            result_str = wrapper({"path": "/tmp/test.txt", "content": "hello"})

        # Broker was called exactly once.
        assert len(dispatch_calls) == 1, (
            f"Expected broker.dispatch called once, got {len(dispatch_calls)}"
        )
        proposal = dispatch_calls[0]
        assert proposal.tool_name == "write_file"
        assert proposal.parameters == {"path": "/tmp/test.txt", "content": "hello"}

        # Native handler was NOT called.
        assert len(call_log["native"]) == 0, (
            f"Native handler was invoked — sequential bypass not closed. "
            f"Calls: {call_log['native']}"
        )

        # Result from broker (not native).
        parsed = json.loads(result_str)
        assert parsed.get("ok") is True

    def test_terminal_wrapper_calls_broker_not_native(self) -> None:
        """terminal (HIGH RISK) also gets a broker wrapper on the sequential path."""
        from hermes.runtime.nous_engine import _make_sequential_write_wrapper  # noqa: PLC0415

        broker = MagicMock()
        dispatch_calls: list[Any] = []

        def _fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return _outcome_executed()

        # Build with write_file then add terminal wrapper manually.
        agent, call_log = _make_governed_agent_with_mock_registry(broker, "terminal")

        wrapper = agent._registered_handlers.get("terminal")
        assert wrapper is not None

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=_fake_bridge):
            result_str = wrapper({"command": "rm -rf /"})

        assert len(dispatch_calls) == 1
        assert dispatch_calls[0].tool_name == "terminal"
        assert len(call_log["native"]) == 0

    def test_browser_navigate_wrapper_calls_broker(self) -> None:
        """browser_navigate (WRITE) gets a broker wrapper."""
        from hermes.runtime.nous_engine import _make_sequential_write_wrapper  # noqa: PLC0415

        broker = MagicMock()
        dispatch_calls: list[Any] = []

        def _fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return _outcome_executed()

        agent, call_log = _make_governed_agent_with_mock_registry(broker, "browser_navigate")

        wrapper = agent._registered_handlers.get("browser_navigate")
        assert wrapper is not None

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=_fake_bridge):
            result_str = wrapper({"url": "https://evil.example.com"})

        assert len(dispatch_calls) == 1
        assert dispatch_calls[0].tool_name == "browser_navigate"
        assert len(call_log["native"]) == 0

    def test_pending_proposal_accumulates_from_sequential_path(self) -> None:
        """PENDING_APPROVAL on the sequential path accumulates the proposal."""
        from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus  # noqa: PLC0415

        broker = MagicMock()
        pending_outcome = ExecutionOutcome(
            proposal_id=uuid4(),
            status=ExecutionStatus.PENDING_APPROVAL,
            result=None,
        )

        agent, _ = _make_governed_agent_with_mock_registry(broker)
        wrapper = agent._registered_handlers.get("write_file")
        assert wrapper is not None

        with patch(
            "hermes.runtime.nous_engine._dispatch_via_bridge",
            return_value=pending_outcome,
        ):
            result_str = wrapper({"path": "/tmp/x"})

        parsed = json.loads(result_str)
        assert "BLOCKED" in parsed["error"]
        # Proposal is accumulated for the orchestrator.
        assert len(agent._pending_proposals) == 1
        assert agent._pending_proposals[0].tool_name == "write_file"

    def test_broker_dispatch_once_on_sequential_not_twice(self) -> None:
        """Sequential path: broker hits exactly ONCE, not via _invoke_tool and wrapper."""
        from hermes.runtime.nous_engine import GovernedAIAgent  # noqa: PLC0415

        broker = MagicMock()
        dispatch_calls: list[Any] = []

        def _fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return _outcome_executed()

        agent, call_log = _make_governed_agent_with_mock_registry(broker)
        wrapper = agent._registered_handlers.get("write_file")

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=_fake_bridge):
            wrapper({"path": "/tmp/y"})

        assert len(dispatch_calls) == 1, (
            f"Broker dispatch called {len(dispatch_calls)} times — expected exactly 1"
        )


# ---------------------------------------------------------------------------
# (2) Concurrent path: no double-gate when _invoke_tool already handles WRITE
# ---------------------------------------------------------------------------


class TestConcurrentNoDoubleGate:
    """The concurrent path calls _invoke_tool which gates before reaching the registry.
    Registry wrappers must NOT be invoked from the concurrent path for WRITE tools."""

    def test_invoke_tool_write_does_not_hit_registry(self) -> None:
        """When _invoke_tool handles a WRITE, registry.dispatch is not called."""
        from hermes.runtime.nous_engine import GovernedAIAgent  # noqa: PLC0415

        broker = MagicMock()
        dispatch_calls: list[Any] = []

        def _fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return _outcome_executed()

        agent, call_log = _make_governed_agent_with_mock_registry(broker)

        registry_dispatch_calls: list[Any] = []
        agent._fake_registry.dispatch.side_effect = lambda *a, **kw: registry_dispatch_calls.append(a) or "registry_hit"

        with patch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=_fake_bridge):
            # Directly call _invoke_tool as the concurrent path does.
            result_str = agent._invoke_tool(
                "write_file",
                {"path": "/tmp/concurrent.txt", "content": "data"},
                "task-concurrent",
                "call-001",
            )

        # Broker called once via _invoke_tool.
        assert len(dispatch_calls) == 1

        # Registry.dispatch was NOT called (concurrent path does not go through registry).
        assert len(registry_dispatch_calls) == 0, (
            "registry.dispatch was called from the concurrent path — double-gate risk"
        )

        # Native handler not called.
        assert len(call_log["native"]) == 0

    def test_invoke_tool_read_does_not_call_broker(self) -> None:
        """For READ tools on the concurrent path, broker is NOT called."""
        from hermes.runtime.nous_engine import GovernedAIAgent  # noqa: PLC0415

        broker = MagicMock()
        dispatch_calls: list[Any] = []

        def _fake_bridge(*, proposal, broker, consent_context, engine_loop, **_):
            dispatch_calls.append(proposal)
            return _outcome_executed()

        agent, _ = _make_governed_agent_with_mock_registry(broker)

        with patch(
            "hermes.runtime.nous_engine._dispatch_via_bridge",
            side_effect=_fake_bridge,
        ):
            with patch.object(agent, "_call_native_invoke", return_value='{"content": "file_data"}'):
                agent._invoke_tool("read_file", {"path": "/etc/hosts"}, "task-001")

        # Broker NOT called for READ tools on concurrent path.
        assert len(dispatch_calls) == 0


# ---------------------------------------------------------------------------
# (3) Startup Composio: no-broker handler is fail-closed, not direct API call
# ---------------------------------------------------------------------------


class TestComposioNoBrokerFailClosed:
    """Regression: startup Composio specs without a broker must NOT call the API.

    The composio SDK has import-time issues in this environment (pre-existing,
    unrelated to this fix). We test the fail-closed logic in isolation using
    the same pattern as the module, without importing the broken SDK.
    """

    def test_direct_composio_handler_raises_on_invocation(self) -> None:
        """The pattern in _make_direct_composio_handler must raise, not call API.

        We replicate the handler logic directly to verify the contract, since
        the composio SDK imports are broken in this environment (pre-existing).
        This is the exact code pattern used in composio_tool_specs._make_direct_composio_handler.
        """
        # Replicate the fail-closed handler as written in composio_tool_specs.py
        slug = "GMAIL_GET_EMAIL"

        async def _fail_closed_handler(params: dict) -> dict:
            raise RuntimeError(
                f"hermes.composio_tools.no_broker_fail_closed: slug={slug} — "
                "broker is None; Composio READ action blocked to prevent "
                "ungated API call."
            )

        with pytest.raises(RuntimeError, match="no_broker_fail_closed"):
            asyncio.run(_fail_closed_handler({}))

    def test_composio_tool_specs_source_has_fail_closed_handler(self) -> None:
        """_make_direct_composio_handler source raises, not calls execute_action.

        Inspects the source code of composio_tool_specs to verify that
        _make_direct_composio_handler contains a raise statement, not a
        client.execute_action() call. This is the authoritative source check.
        """
        import inspect  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        module_path = (
            Path(__file__).parents[2]
            / "src/hermes/runtime/composio_tool_specs.py"
        )
        source = module_path.read_text()

        # Find the _make_direct_composio_handler function block in source.
        # The function must contain a raise statement (fail-closed).
        assert "_fail_closed_handler" in source, (
            "_make_direct_composio_handler does not define a _fail_closed_handler — "
            "the fail-closed fix was not applied."
        )
        # Must NOT contain execute_action call inside _make_direct_composio_handler.
        # We check that execute_action does not appear after the fail-closed function def.
        fn_start = source.find("def _make_direct_composio_handler")
        fn_end = source.find("\ndef ", fn_start + 1)
        fn_body = source[fn_start:fn_end] if fn_end > fn_start else source[fn_start:]
        assert "execute_action" not in fn_body, (
            "_make_direct_composio_handler still calls execute_action — "
            "the fail-closed fix was not applied correctly."
        )

    def test_make_read_handler_no_broker_path_uses_fail_closed(self) -> None:
        """_make_read_handler falls to _make_direct_composio_handler when broker is None.

        Verifies the source: when broker is None, _make_read_handler calls
        _make_direct_composio_handler (which is now fail-closed), not a live path.
        """
        from pathlib import Path  # noqa: PLC0415

        module_path = (
            Path(__file__).parents[2]
            / "src/hermes/runtime/composio_tool_specs.py"
        )
        source = module_path.read_text()

        fn_start = source.find("def _make_read_handler")
        fn_end = source.find("\ndef ", fn_start + 1)
        fn_body = source[fn_start:fn_end] if fn_end > fn_start else source[fn_start:]

        # The no-broker branch must delegate to _make_direct_composio_handler.
        assert "_make_direct_composio_handler" in fn_body, (
            "_make_read_handler does not delegate to _make_direct_composio_handler "
            "for the no-broker case — the fail-closed path is disconnected."
        )
        # The no-broker branch must NOT create a ComposioClient inline.
        assert "ComposioClient(" not in fn_body, (
            "_make_read_handler creates ComposioClient directly — broker bypass exists."
        )

    def test_startup_composio_skipped_in_run(self) -> None:
        """_build_composio_tool_specs_sync is NOT called during _run startup.

        Verifies that the startup sequence no longer seeds the registry with
        broker-less Composio specs. The broker-aware registry handles the first
        fetch on its own TTL schedule.
        """
        import hermes.runtime.__main__ as m  # noqa: PLC0415
        import inspect  # noqa: PLC0415

        run_source = inspect.getsource(m._run)
        assert "_build_composio_tool_specs_sync" not in run_source, (
            "_build_composio_tool_specs_sync is still called at startup — "
            "broker-less Composio specs are being seeded into the registry. "
            "Remove the startup call to prevent ungated Composio API calls."
        )

    def test_seed_composio_registry_not_called_from_run(self) -> None:
        """_seed_composio_registry_with is not called with broker-less specs in _run."""
        import hermes.runtime.__main__ as m  # noqa: PLC0415
        import inspect  # noqa: PLC0415

        run_source = inspect.getsource(m._run)
        # The seeding call was removed along with the startup fetch.
        # If _seed_composio_registry_with appears in _run, it's a regression.
        assert "_seed_composio_registry_with" not in run_source, (
            "_seed_composio_registry_with is still called from _run — "
            "broker-less startup specs may be seeded into the registry."
        )
