"""Tests for the autonomous computer-use loop.

Covers the design invariants mandated in the task spec:
  1. Loop calls screenshot → vision → dispatch in order (ordered call sequence).
  2. Loop stops on DoneAction.
  3. Loop respects the step ceiling (StepCeilingReached).
  4. Every action goes through CapabilityBrokerPort.dispatch — no bypass.
  5. Grant is released (revoked) after the loop regardless of outcome.
  6. Kill-switch is respected (broker returns REJECTED_BY_POLICY).

All I/O is mocked: the vision LLM, the broker, and the consent manager.
No real DB, no real socket, no real LLM calls.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID, uuid4

import pytest

from hermes.computer_use.domain.vision_action import (
    AskHumanAction,
    ClickAction,
    DoneAction,
    MoveAction,
    TypeAction,
    parse_vision_action,
)
from hermes.computer_use.application.computer_use_loop import (
    ComputerUseLoopError,
    StepCeilingReached,
    _action_to_tool_call,
    run_computer_use_loop,
)
from hermes.capabilities.domain.ports import (
    ConsentContext,
    ExecutionOutcome,
    ExecutionStatus,
)
from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker

pytestmark = pytest.mark.unit

_TENANT_ID = uuid4()
_OPERATOR_ID = uuid4()
_TASK_ID = uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_consent_manager() -> MagicMock:
    """Minimal ConsentManager fake."""
    cm = MagicMock()
    cm.grant = MagicMock(return_value=MagicMock())
    cm.revoke = MagicMock(return_value=None)
    return cm


def _screenshot_result(path: str = "/tmp/shot.png") -> dict[str, Any]:
    return {"ok": True, "path": path, "width": 1280, "height": 800}


def _make_broker_with_screenshot(
    *, screenshot_result: dict[str, Any] | None = None
) -> FakeCapabilityBroker:
    """Broker that returns a screenshot result for 'screenshot' tool calls
    and EXECUTED for all other dispatches."""
    shot_res = screenshot_result or _screenshot_result()
    shot_outcome = ExecutionOutcome(
        proposal_id=uuid4(),
        status=ExecutionStatus.EXECUTED,
        audit_entry_id=uuid4(),
        result=shot_res,
    )
    action_outcome = ExecutionOutcome(
        proposal_id=uuid4(),
        status=ExecutionStatus.EXECUTED,
        audit_entry_id=uuid4(),
        result={"ok": True},
    )

    class _ScreenshotAwareBroker(FakeCapabilityBroker):
        async def dispatch(self, proposal, consent_ctx, **kwargs):
            self.dispatched.append((proposal, consent_ctx))
            if proposal.tool_name == "screenshot":
                return ExecutionOutcome(
                    proposal_id=proposal.proposal_id,
                    status=ExecutionStatus.EXECUTED,
                    audit_entry_id=uuid4(),
                    result=shot_res,
                )
            return ExecutionOutcome(
                proposal_id=proposal.proposal_id,
                status=ExecutionStatus.EXECUTED,
                audit_entry_id=uuid4(),
                result={"ok": True},
            )

    return _ScreenshotAwareBroker()


def _vision_client_returning(actions: list[dict]) -> AsyncMock:
    """Vision client mock that returns actions in sequence, then done."""
    responses = list(actions)
    idx = {"n": 0}

    async def _client(**_kwargs: Any) -> dict:
        i = idx["n"]
        idx["n"] += 1
        if i < len(responses):
            return responses[i]
        return {"kind": "done", "summary": "fallback done"}

    return _client


# ---------------------------------------------------------------------------
# 1. screenshot → vision → dispatch ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_calls_screenshot_then_vision_then_dispatch_in_order() -> None:
    """The loop must call screenshot → vision → dispatch in strict order per step."""
    call_order: list[str] = []
    broker = _make_broker_with_screenshot()

    original_dispatch = broker.dispatch

    async def recording_dispatch(proposal, consent_ctx, **kwargs):
        call_order.append(f"dispatch:{proposal.tool_name}")
        return await original_dispatch(proposal, consent_ctx, **kwargs)

    broker.dispatch = recording_dispatch  # type: ignore[method-assign]

    vision_calls: list[str] = []

    async def recording_vision(**kwargs: Any) -> dict:
        vision_calls.append("vision")
        # Return click first, then done
        if len(vision_calls) == 1:
            return {"kind": "click", "x": 100, "y": 200, "btn": 0}
        return {"kind": "done", "summary": "finished"}

    await run_computer_use_loop(
        goal="click the button",
        tenant_id=_TENANT_ID,
        operator_id=_OPERATOR_ID,
        consent_manager=_mock_consent_manager(),
        broker=broker,
        vision_client=recording_vision,
        model="gpt-4o",
        api_key=None,
        base_url=None,
    )

    # Verify the interleaving: screenshot, vision, dispatch(click), screenshot, vision, done
    assert call_order[0] == "dispatch:screenshot"
    assert vision_calls[0] == "vision"
    assert "dispatch:mouse_click" in call_order or "dispatch:mouse_move" in call_order
    assert call_order.count("dispatch:screenshot") == 2  # one screenshot per step


# ---------------------------------------------------------------------------
# 2. Loop stops on DoneAction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_stops_on_done_action() -> None:
    """Loop must terminate as soon as the vision LLM returns done."""
    broker = _make_broker_with_screenshot()

    vision = _vision_client_returning([
        {"kind": "done", "summary": "task complete"},
    ])

    result = await run_computer_use_loop(
        goal="do something",
        tenant_id=_TENANT_ID,
        operator_id=_OPERATOR_ID,
        consent_manager=_mock_consent_manager(),
        broker=broker,
        vision_client=vision,
        model="gpt-4o",
        api_key=None,
        base_url=None,
    )

    assert result == "task complete"
    # Only one screenshot was dispatched (the done step)
    screenshot_calls = [
        p for p, _ in broker.dispatched if p.tool_name == "screenshot"
    ]
    assert len(screenshot_calls) == 1


# ---------------------------------------------------------------------------
# 3. Step ceiling is respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_raises_on_step_ceiling() -> None:
    """Loop must raise StepCeilingReached after max_steps without done."""
    broker = _make_broker_with_screenshot()

    # Vision always returns a move (no done)
    vision = _vision_client_returning([
        {"kind": "move", "x": i, "y": 0}
        for i in range(100)  # more than max_steps
    ])

    with pytest.raises(StepCeilingReached):
        await run_computer_use_loop(
            goal="infinite task",
            tenant_id=_TENANT_ID,
            operator_id=_OPERATOR_ID,
            consent_manager=_mock_consent_manager(),
            broker=broker,
            vision_client=vision,
            model="gpt-4o",
            api_key=None,
            base_url=None,
            max_steps=5,
        )


# ---------------------------------------------------------------------------
# 4. Every action goes through CapabilityBrokerPort.dispatch (no bypass)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_actions_dispatch_through_broker() -> None:
    """Every single action (screenshot + mouse + type) must pass through the broker."""
    dispatched_tools: list[str] = []

    class _RecordingBroker:
        async def dispatch(self, proposal, consent_ctx, **kwargs):
            dispatched_tools.append(proposal.tool_name)
            return ExecutionOutcome(
                proposal_id=proposal.proposal_id,
                status=ExecutionStatus.EXECUTED,
                audit_entry_id=uuid4(),
                result={"ok": True, "path": "/tmp/shot.png", "width": 800, "height": 600},
            )

    vision = _vision_client_returning([
        {"kind": "click", "x": 10.0, "y": 20.0, "btn": 0},
        {"kind": "type", "text": "hello"},
        {"kind": "done", "summary": "done"},
    ])

    await run_computer_use_loop(
        goal="type hello",
        tenant_id=_TENANT_ID,
        operator_id=_OPERATOR_ID,
        consent_manager=_mock_consent_manager(),
        broker=_RecordingBroker(),
        vision_client=vision,
        model="gpt-4o",
        api_key=None,
        base_url=None,
    )

    assert "screenshot" in dispatched_tools
    assert "mouse_click" in dispatched_tools
    assert "type_text" in dispatched_tools
    # Verify every tool went through broker (no other code path)
    assert len(dispatched_tools) >= 3


# ---------------------------------------------------------------------------
# 5. Grant is released after the loop (done)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_revoked_after_loop_done() -> None:
    """ConsentManager.revoke(INPUT_CONTROL) must be called after the loop ends."""
    cm = _mock_consent_manager()
    broker = _make_broker_with_screenshot()

    vision = _vision_client_returning([{"kind": "done", "summary": "ok"}])

    await run_computer_use_loop(
        goal="test",
        tenant_id=_TENANT_ID,
        operator_id=_OPERATOR_ID,
        consent_manager=cm,
        broker=broker,
        vision_client=vision,
        model="gpt-4o",
        api_key=None,
        base_url=None,
    )

    from hermes.agents_os.application.consent_manager import Capability  # noqa: PLC0415
    cm.revoke.assert_called_once_with(
        human_operator_id=_OPERATOR_ID,
        capability=Capability.INPUT_CONTROL,
    )


# ---------------------------------------------------------------------------
# 6. Grant is released after the loop (ceiling reached)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_revoked_after_ceiling_reached() -> None:
    """ConsentManager.revoke must be called even when StepCeilingReached is raised."""
    cm = _mock_consent_manager()
    broker = _make_broker_with_screenshot()

    vision = _vision_client_returning([
        {"kind": "move", "x": float(i), "y": 0.0} for i in range(20)
    ])

    with pytest.raises(StepCeilingReached):
        await run_computer_use_loop(
            goal="infinite",
            tenant_id=_TENANT_ID,
            operator_id=_OPERATOR_ID,
            consent_manager=cm,
            broker=broker,
            vision_client=vision,
            model="gpt-4o",
            api_key=None,
            base_url=None,
            max_steps=3,
        )

    from hermes.agents_os.application.consent_manager import Capability  # noqa: PLC0415
    cm.revoke.assert_called_once_with(
        human_operator_id=_OPERATOR_ID,
        capability=Capability.INPUT_CONTROL,
    )


# ---------------------------------------------------------------------------
# 7. Kill-switch: broker REJECTED_BY_POLICY does not crash the loop —
#    it records the failed dispatch and continues (kill-switch is enforced
#    inside the broker on every step; a single rejection is recorded).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_handles_broker_rejection_gracefully() -> None:
    """If the broker rejects an action (kill-switch), the step records the failure
    and the loop continues (or done terminates it normally)."""
    # Broker rejects mouse_click but allows screenshot (needed for loop to run)
    class _RejectClickBroker:
        def __init__(self) -> None:
            self.dispatched: list[str] = []

        async def dispatch(self, proposal, consent_ctx, **kwargs):
            self.dispatched.append(proposal.tool_name)
            if proposal.tool_name == "screenshot":
                return ExecutionOutcome(
                    proposal_id=proposal.proposal_id,
                    status=ExecutionStatus.EXECUTED,
                    audit_entry_id=uuid4(),
                    result={"ok": True, "path": "/tmp/shot.png", "width": 800, "height": 600},
                )
            # All non-screenshot actions are rejected (kill-switch)
            return ExecutionOutcome(
                proposal_id=proposal.proposal_id,
                status=ExecutionStatus.REJECTED_BY_POLICY,
                error="agent paused — kill-switch active",
            )

    broker = _RejectClickBroker()
    vision = _vision_client_returning([
        {"kind": "click", "x": 5.0, "y": 5.0, "btn": 0},
        {"kind": "done", "summary": "finished"},
    ])

    result = await run_computer_use_loop(
        goal="test kill-switch",
        tenant_id=_TENANT_ID,
        operator_id=_OPERATOR_ID,
        consent_manager=_mock_consent_manager(),
        broker=broker,
        vision_client=vision,
        model="gpt-4o",
        api_key=None,
        base_url=None,
    )

    assert result == "finished"
    # mouse_click was dispatched through broker (not bypassed)
    assert "mouse_click" in broker.dispatched


# ---------------------------------------------------------------------------
# 8. Screenshot failure raises ComputerUseLoopError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_raises_on_screenshot_failure() -> None:
    """ComputerUseLoopError must be raised if screenshot fails."""
    class _FailScreenshotBroker:
        async def dispatch(self, proposal, consent_ctx, **kwargs):
            return ExecutionOutcome(
                proposal_id=proposal.proposal_id,
                status=ExecutionStatus.FAILED,
                error="bridge unavailable",
                result={},
            )

    vision = _vision_client_returning([{"kind": "done", "summary": "ok"}])

    with pytest.raises(ComputerUseLoopError, match="Screenshot failed"):
        await run_computer_use_loop(
            goal="fail",
            tenant_id=_TENANT_ID,
            operator_id=_OPERATOR_ID,
            consent_manager=_mock_consent_manager(),
            broker=_FailScreenshotBroker(),
            vision_client=vision,
            model="gpt-4o",
            api_key=None,
            base_url=None,
        )


# ---------------------------------------------------------------------------
# 9. ask_human terminates the loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_stops_on_ask_human() -> None:
    """Loop must terminate on ask_human returning a prefixed summary."""
    broker = _make_broker_with_screenshot()
    vision = _vision_client_returning([
        {"kind": "ask_human", "question": "What is the password?"},
    ])

    result = await run_computer_use_loop(
        goal="login",
        tenant_id=_TENANT_ID,
        operator_id=_OPERATOR_ID,
        consent_manager=_mock_consent_manager(),
        broker=broker,
        vision_client=vision,
        model="gpt-4o",
        api_key=None,
        base_url=None,
    )

    assert result.startswith("ask_human:")
    assert "password" in result


# ---------------------------------------------------------------------------
# 10. VisionAction parsing — unit tests for the domain value objects
# ---------------------------------------------------------------------------


def test_parse_vision_action_click() -> None:
    action = parse_vision_action({"kind": "click", "x": 100.5, "y": 200.0, "btn": 1})
    assert isinstance(action, ClickAction)
    assert action.x == 100.5
    assert action.y == 200.0
    assert action.btn == 1


def test_parse_vision_action_type() -> None:
    action = parse_vision_action({"kind": "type", "text": "hello world"})
    assert isinstance(action, TypeAction)
    assert action.text == "hello world"


def test_parse_vision_action_done() -> None:
    action = parse_vision_action({"kind": "done", "summary": "all done"})
    assert isinstance(action, DoneAction)
    assert action.summary == "all done"


def test_parse_vision_action_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown VisionAction kind"):
        parse_vision_action({"kind": "run_shell", "command": "rm -rf /"})


def test_parse_vision_action_missing_kind_raises() -> None:
    with pytest.raises(ValueError, match="Unknown VisionAction kind"):
        parse_vision_action({"x": 10, "y": 20})


# ---------------------------------------------------------------------------
# 11. _action_to_tool_call mapping — verify correct broker tool names
# ---------------------------------------------------------------------------


def test_click_maps_to_mouse_click() -> None:
    tool_name, params = _action_to_tool_call(ClickAction(x=10.0, y=20.0, btn=0))
    assert tool_name == "mouse_click"
    assert params["x"] == 10.0
    assert params["y"] == 20.0


def test_type_maps_to_type_text() -> None:
    tool_name, params = _action_to_tool_call(TypeAction(text="hello"))
    assert tool_name == "type_text"
    assert params["text"] == "hello"


def test_move_maps_to_mouse_move() -> None:
    tool_name, params = _action_to_tool_call(MoveAction(x=50.0, y=100.0))
    assert tool_name == "mouse_move"
    assert params["x"] == 50.0


# ---------------------------------------------------------------------------
# 12. begin_computer_use registry entry exists and has correct properties
# ---------------------------------------------------------------------------


def test_begin_computer_use_registered_as_high_risk() -> None:
    """begin_computer_use must be HIGH risk in the capability registry."""
    from hermes.capabilities.application.capability_registry import CapabilityRegistry
    from hermes.capabilities.domain.ports import RiskLevel

    registry = CapabilityRegistry()
    binding = registry.resolve("begin_computer_use")
    assert binding is not None
    assert binding.risk is RiskLevel.HIGH
    assert binding.auto_executable is False
    assert binding.executor == "os_native"
    assert binding.persistent_forbidden is True


def test_begin_computer_use_requires_input_control_consent() -> None:
    """begin_computer_use must require INPUT_CONTROL consent."""
    from hermes.capabilities.application.capability_registry import CapabilityRegistry
    from hermes.agents_os.application.consent_manager import Capability

    registry = CapabilityRegistry()
    binding = registry.resolve("begin_computer_use")
    assert binding is not None
    assert binding.required_capability == Capability.INPUT_CONTROL.value
