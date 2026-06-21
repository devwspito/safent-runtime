"""Tests de lógica pura para ChatTurnStateMachine y AutoscrollTracker.

No requieren GTK ni display. Se ejecutan en cualquier entorno.
"""

from __future__ import annotations

import pytest

from hermes.shell.presentation.gtk4.chat_turn_state import (
    AutoscrollTracker,
    ChatTurnStateMachine,
    TurnState,
)

pytestmark = pytest.mark.unit


# -----------------------------------------------------------------------
# ChatTurnStateMachine — flujo feliz
# -----------------------------------------------------------------------

class TestHappyPath:
    def test_initial_state_is_idle(self) -> None:
        sm = ChatTurnStateMachine()
        assert sm.state == TurnState.IDLE

    def test_user_message_transitions_to_user_pinned(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("hola")
        assert sm.state == TurnState.USER_PINNED
        assert sm.last_user_text == "hola"

    def test_enqueue_ok_transitions_to_awaiting(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_ok()
        assert sm.state == TurnState.AWAITING_FIRST_TOKEN

    def test_first_delta_transitions_to_streaming(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_ok()
        sm.on_first_delta()
        assert sm.state == TurnState.STREAMING

    def test_done_transitions_to_idle(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_ok()
        sm.on_first_delta()
        sm.on_done()
        assert sm.state == TurnState.IDLE

    def test_tool_call_transitions_to_tool_running(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_ok()
        sm.on_first_delta()
        sm.on_tool_call()
        assert sm.state == TurnState.TOOL_RUNNING

    def test_approval_needed_transitions_to_awaiting_approval(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_ok()
        sm.on_first_delta()
        sm.on_tool_call()
        sm.on_approval_needed()
        assert sm.state == TurnState.AWAITING_APPROVAL


# -----------------------------------------------------------------------
# Stop button visibility
# -----------------------------------------------------------------------

class TestStopButton:
    def test_stop_button_hidden_in_idle(self) -> None:
        sm = ChatTurnStateMachine()
        assert not sm.show_stop_button

    def test_stop_button_visible_after_user_message(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("hola")
        assert sm.show_stop_button

    def test_stop_button_visible_during_streaming(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("hola")
        sm.on_enqueue_ok()
        sm.on_first_delta()
        assert sm.show_stop_button

    def test_stop_button_hidden_after_done(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("hola")
        sm.on_enqueue_ok()
        sm.on_first_delta()
        sm.on_done()
        assert not sm.show_stop_button


# -----------------------------------------------------------------------
# Error path
# -----------------------------------------------------------------------

class TestErrorPath:
    def test_enqueue_fail_transitions_to_turn_error(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_fail()
        assert sm.state == TurnState.TURN_ERROR

    def test_error_frame_during_streaming_goes_to_turn_error(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_ok()
        sm.on_first_delta()
        sm.on_error_frame()
        assert sm.state == TurnState.TURN_ERROR

    def test_retry_from_error_goes_to_user_pinned(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_fail()
        sm.on_retry()
        assert sm.state == TurnState.USER_PINNED

    def test_stream_interrupted_transitions(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_ok()
        sm.on_first_delta()
        sm.on_stream_interrupted()
        assert sm.state == TurnState.INTERRUPTED

    def test_retry_from_interrupted_goes_to_user_pinned(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_ok()
        sm.on_stream_interrupted()
        sm.on_retry()
        assert sm.state == TurnState.USER_PINNED


# -----------------------------------------------------------------------
# Stop (usuario pulsa Detener / Esc)
# -----------------------------------------------------------------------

class TestStop:
    def test_stop_from_streaming_goes_to_idle(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_ok()
        sm.on_first_delta()
        sm.on_stop()
        assert sm.state == TurnState.IDLE

    def test_stop_from_awaiting_goes_to_idle(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_ok()
        sm.on_stop()
        assert sm.state == TurnState.IDLE

    def test_stop_from_idle_is_noop(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_stop()
        assert sm.state == TurnState.IDLE


# -----------------------------------------------------------------------
# No-model flow
# -----------------------------------------------------------------------

class TestNoModel:
    def test_no_model_sent_stores_pending_text(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_no_model_sent("recordatorio semanal")
        assert sm.pending_no_model_text == "recordatorio semanal"
        assert sm.state == TurnState.IDLE

    def test_model_connected_returns_true_if_pending(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_no_model_sent("tarea pendiente")
        assert sm.on_model_connected() is True

    def test_model_connected_returns_false_if_no_pending(self) -> None:
        sm = ChatTurnStateMachine()
        assert sm.on_model_connected() is False

    def test_clear_pending_returns_and_removes_text(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_no_model_sent("algo importante")
        text = sm.clear_pending_no_model()
        assert text == "algo importante"
        assert sm.pending_no_model_text == ""
        assert sm.on_model_connected() is False


# -----------------------------------------------------------------------
# is_live property
# -----------------------------------------------------------------------

class TestIsLive:
    def test_not_live_in_idle(self) -> None:
        sm = ChatTurnStateMachine()
        assert not sm.is_live

    def test_not_live_after_user_pinned(self) -> None:
        # USER_PINNED no cuenta como "live" hasta que enqueue OK
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        assert not sm.is_live

    def test_live_during_awaiting_first_token(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_ok()
        assert sm.is_live

    def test_live_during_streaming(self) -> None:
        sm = ChatTurnStateMachine()
        sm.on_user_message("test")
        sm.on_enqueue_ok()
        sm.on_first_delta()
        assert sm.is_live


# -----------------------------------------------------------------------
# AutoscrollTracker
# -----------------------------------------------------------------------

class TestAutoscrollTracker:
    def test_initially_stuck(self) -> None:
        tracker = AutoscrollTracker()
        assert tracker.stuck_to_bottom

    def test_stuck_when_at_bottom(self) -> None:
        tracker = AutoscrollTracker()
        # upper=1000, page=600, value=400 → distance=0
        tracker.update(value=400, page_size=600, upper=1000)
        assert tracker.stuck_to_bottom

    def test_stuck_within_threshold(self) -> None:
        tracker = AutoscrollTracker()
        # upper=1000, page=600, value=352 → distance=48 (justo en el umbral)
        tracker.update(value=352, page_size=600, upper=1000)
        assert tracker.stuck_to_bottom

    def test_not_stuck_above_threshold(self) -> None:
        tracker = AutoscrollTracker()
        # upper=1000, page=600, value=300 → distance=100 > 48
        tracker.update(value=300, page_size=600, upper=1000)
        assert not tracker.stuck_to_bottom

    def test_force_stick_resets_to_stuck(self) -> None:
        tracker = AutoscrollTracker()
        tracker.update(value=0, page_size=600, upper=1000)
        assert not tracker.stuck_to_bottom
        tracker.force_stick()
        assert tracker.stuck_to_bottom

    def test_user_scroll_up_unsticks(self) -> None:
        tracker = AutoscrollTracker()
        # Primero en el fondo
        tracker.update(value=400, page_size=600, upper=1000)
        assert tracker.stuck_to_bottom
        # Usuario sube
        tracker.update(value=200, page_size=600, upper=1000)
        assert not tracker.stuck_to_bottom
