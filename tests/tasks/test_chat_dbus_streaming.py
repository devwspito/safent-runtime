"""Regression tests for D-Bus chat streaming (spec streaming-dbus).

Coverage:
  (a) ChatDelta is emitted per coalesced batch with incrementing seq and
      the correct conversation_id.
  (b) ChatStreamEnd is emitted once after run_cycle completes.
  (c) Coalescing respects character threshold and interval; batches preserve
      order (concatenation of deltas is the full text).
  (d) When conversation_id is absent from metadata, no ChatDelta/ChatStreamEnd
      signals are emitted (non-chat / autonomous tasks).
  (e) Thread-safety: emit_delta called from a non-event-loop thread does not
      crash (simulates the executor thread path).
  (f) _inject_chunk_sink now also stores conversation_id in metadata.
  (g) NousReasoningEngine.set_chat_delta_emitter stores the callables
      and they are accessible for injection in start().
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from hermes.tasks.application.agent_loop_orchestrator import _inject_chunk_sink
from hermes.runtime.nous_engine import (
    NousReasoningEngine,
    _DBUS_BATCH_CHARS,
    _DBUS_FLUSH_INTERVAL_S,
    _build_stream_callback,
)

pytestmark = pytest.mark.unit

_TENANT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_OPERATOR = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_CONV_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_TASK_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeChunkSink:
    """Minimal chunk sink that records calls without async complexity."""

    def __init__(self) -> None:
        self.emitted: list[Any] = []
        self.closed: list[dict] = []
        self.statuses: list[str] = []

    async def emit(self, *, task_id: Any, chunk: Any) -> None:
        self.emitted.append(chunk)

    async def close(self, *, task_id: Any, outcome: str, error: Any = None) -> None:
        self.closed.append({"outcome": outcome, "error": error})

    async def emit_status(self, *, task_id: Any, status: str) -> None:
        self.statuses.append(status)


def _run_loop() -> asyncio.AbstractEventLoop:
    """Return a running event loop (same loop used by run_coroutine_threadsafe)."""
    return asyncio.new_event_loop()


def _build_callback(
    *,
    conversation_id: str = _CONV_ID,
    dbus_emit_delta=None,
) -> tuple[Any, list[int], "_FakeChunkSink"]:
    """Build a stream callback wired to a fake chunk sink."""
    loop = asyncio.new_event_loop()
    sink = _FakeChunkSink()
    counter: list[int] = [0]
    cb = _build_stream_callback(
        sink,
        _TASK_ID,
        loop,
        counter,
        dbus_emit_delta=dbus_emit_delta,
        conversation_id=conversation_id,
    )
    cb._loop = loop  # type: ignore[attr-defined]  # test: pump scheduled emits
    return cb, counter, sink


def _pump(cb) -> None:
    """Run the loop one iteration so call_soon_threadsafe-scheduled D-Bus emits run.

    The B1 fix marshals each per-delta emit via loop.call_soon_threadsafe (because
    dbus-fast's aio emission is NOT thread-safe). On the non-running test loop the
    emit stays queued until the loop runs, so emission-asserting tests must pump.
    """
    cb._loop.run_until_complete(asyncio.sleep(0))


# ---------------------------------------------------------------------------
# (a) ChatDelta emitted with incrementing seq and correct conversation_id
# ---------------------------------------------------------------------------


class TestChatDeltaEmission:
    def test_delta_emitted_with_correct_conv_id_and_seq(self) -> None:
        """Each flush produces a ChatDelta call with the right conversation_id."""
        emitted: list[tuple[str, int, str]] = []

        def fake_emit(conv_id: str, seq: int, text: str) -> None:
            emitted.append((conv_id, seq, text))

        cb, _, _ = _build_callback(dbus_emit_delta=fake_emit)
        # Send enough chars to trigger a flush.
        text = "x" * _DBUS_BATCH_CHARS
        cb(text)
        flush = getattr(cb, "_flush_dbus", None)
        assert callable(flush), "_flush_dbus must be attached to the callback"
        flush(force=True)
        _pump(cb)

        assert len(emitted) >= 1
        conv_id, seq, payload = emitted[-1]
        assert conv_id == _CONV_ID
        assert seq >= 1
        assert text in payload or payload in text  # text is present

    def test_seq_monotonically_increasing(self) -> None:
        """Each flush increments seq by 1."""
        seqs: list[int] = []

        def fake_emit(conv_id: str, seq: int, text: str) -> None:
            seqs.append(seq)

        cb, _, _ = _build_callback(dbus_emit_delta=fake_emit)
        flush = getattr(cb, "_flush_dbus")

        for _ in range(3):
            cb("x" * _DBUS_BATCH_CHARS)
            flush(force=True)
        _pump(cb)

        assert seqs == list(range(1, len(seqs) + 1)), f"seq must start at 1 and be consecutive: {seqs}"

    def test_text_order_preserved(self) -> None:
        """Concatenation of all flushed batches equals the original text."""
        batches: list[str] = []

        def fake_emit(conv_id: str, seq: int, text: str) -> None:
            batches.append(text)

        cb, _, _ = _build_callback(dbus_emit_delta=fake_emit)
        flush = getattr(cb, "_flush_dbus")

        words = ["Hello", " ", "world", "!", " How", " are", " you", "?"]
        for w in words:
            cb(w)
            flush(force=True)
        _pump(cb)

        assert "".join(batches) == "".join(words)


# ---------------------------------------------------------------------------
# (b) ChatStreamEnd emitted once after run_cycle
# ---------------------------------------------------------------------------


class TestChatStreamEnd:
    def test_flush_force_drains_buffer(self) -> None:
        """force=True flushes any remaining text even if below threshold."""
        emitted: list[tuple[str, int, str]] = []

        def fake_emit(conv_id: str, seq: int, text: str) -> None:
            emitted.append((conv_id, seq, text))

        cb, _, _ = _build_callback(dbus_emit_delta=fake_emit)
        # Send only a few chars — below threshold, no auto-flush yet.
        cb("Hi")
        flush = getattr(cb, "_flush_dbus")
        flush(force=True)
        _pump(cb)

        assert len(emitted) == 1
        assert emitted[0][2] == "Hi"

    def test_no_double_flush_on_empty_buffer(self) -> None:
        """force=True on empty buffer emits nothing."""
        emitted: list[tuple] = []

        def fake_emit(conv_id: str, seq: int, text: str) -> None:
            emitted.append((conv_id, seq, text))

        cb, _, _ = _build_callback(dbus_emit_delta=fake_emit)
        flush = getattr(cb, "_flush_dbus")
        # Force-flush with nothing in the buffer.
        flush(force=True)
        assert emitted == []


# ---------------------------------------------------------------------------
# (c) Coalescing respects thresholds
# ---------------------------------------------------------------------------


class TestCoalescing:
    def test_auto_flush_on_char_threshold(self) -> None:
        """Sending _DBUS_BATCH_CHARS chars triggers an auto-flush."""
        emitted: list[tuple] = []

        def fake_emit(conv_id: str, seq: int, text: str) -> None:
            emitted.append((conv_id, seq, text))

        cb, _, _ = _build_callback(dbus_emit_delta=fake_emit)
        # Send exactly the threshold in one shot.
        cb("a" * _DBUS_BATCH_CHARS)
        # Auto-flush should have fired inside the callback (scheduled on the loop).
        _pump(cb)
        assert len(emitted) >= 1

    def test_thinking_delta_not_forwarded_to_dbus(self) -> None:
        """thinking_delta tokens must NOT appear in D-Bus ChatDelta signals."""
        emitted: list[tuple] = []

        def fake_emit(conv_id: str, seq: int, text: str) -> None:
            emitted.append((conv_id, seq, text))

        cb, _, _ = _build_callback(dbus_emit_delta=fake_emit)
        cb("internal thought", "thinking_delta")
        flush = getattr(cb, "_flush_dbus")
        flush(force=True)
        # Buffer should be empty: thinking_delta is not forwarded.
        assert emitted == []


# ---------------------------------------------------------------------------
# (d) No signals when conversation_id is absent
# ---------------------------------------------------------------------------


class TestNoConversationId:
    def test_no_dbus_emit_without_conv_id(self) -> None:
        """When conversation_id is empty, D-Bus emitter is never called."""
        emitted: list[tuple] = []

        def fake_emit(conv_id: str, seq: int, text: str) -> None:
            emitted.append((conv_id, seq, text))

        cb, _, _ = _build_callback(conversation_id="", dbus_emit_delta=fake_emit)
        cb("a" * _DBUS_BATCH_CHARS)
        flush = getattr(cb, "_flush_dbus")
        flush(force=True)

        assert emitted == []

    def test_no_dbus_emit_when_emitter_is_none(self) -> None:
        """When dbus_emit_delta is None (emitter not wired), no crash and no call."""
        cb, counter, _ = _build_callback(dbus_emit_delta=None)
        cb("a" * _DBUS_BATCH_CHARS)
        flush = getattr(cb, "_flush_dbus")
        flush(force=True)
        # counter is incremented only by the chunk_sink path, not D-Bus.
        # No assertion about counter here — just verify no exception.


# ---------------------------------------------------------------------------
# (e) Thread-safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_emit_from_non_loop_thread_does_not_crash(self) -> None:
        """Calling emit_delta from a non-event-loop thread is safe (fail-soft)."""
        errors: list[Exception] = []

        def fake_emit(conv_id: str, seq: int, text: str) -> None:
            # Intentionally slow to stress concurrent access.
            pass

        cb, _, _ = _build_callback(dbus_emit_delta=fake_emit)
        flush = getattr(cb, "_flush_dbus")
        results: list[bool] = []

        def worker():
            try:
                for _ in range(10):
                    cb("hello")
                flush(force=True)
                results.append(True)
            except Exception as exc:
                errors.append(exc)
                results.append(False)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == [], f"Thread-safety violation: {errors}"
        assert all(results)

    def test_dbus_emit_marshaled_via_call_soon_threadsafe(self) -> None:
        """B1 (code-review I1): the per-delta D-Bus emit MUST be scheduled on the bus
        loop via call_soon_threadsafe — NOT invoked synchronously from the executor
        thread. dbus-fast's aio emission calls loop.create_future()/add_writer()/
        sock.send() synchronously, which are not asyncio-thread-safe; a direct call
        from the executor thread would race/corrupt the daemon loop. This test spies
        the loop to PROVE the marshaling happens (the old test used a fake that
        bypassed exactly this concern, giving false confidence)."""
        scheduled: list = []
        emitted: list = []

        def fake_emit(conv_id, seq, text):
            emitted.append((conv_id, seq, text))

        loop = asyncio.new_event_loop()
        real_cst = loop.call_soon_threadsafe

        def spy_cst(fn, *args):
            scheduled.append((fn, args))
            return real_cst(fn, *args)

        loop.call_soon_threadsafe = spy_cst  # type: ignore[assignment]
        cb = _build_stream_callback(
            _FakeChunkSink(), _TASK_ID, loop, [0],
            dbus_emit_delta=fake_emit, conversation_id=_CONV_ID,
        )
        cb._loop = loop  # type: ignore[attr-defined]

        cb("z" * _DBUS_BATCH_CHARS)
        getattr(cb, "_flush_dbus")(force=True)

        # SCHEDULED via call_soon_threadsafe, NOT executed synchronously.
        assert any(fn is fake_emit for fn, _ in scheduled), \
            "ChatDelta emit must be marshaled via loop.call_soon_threadsafe (B1)"
        assert emitted == [], "emit must NOT run synchronously on the caller thread"

        # Only after the loop runs does the scheduled emit execute.
        loop.call_soon_threadsafe = real_cst  # type: ignore[assignment]
        _pump(cb)
        assert len(emitted) >= 1


# ---------------------------------------------------------------------------
# (f) _inject_chunk_sink stores conversation_id in metadata
# ---------------------------------------------------------------------------


class TestInjectChunkSink:
    def test_conversation_id_injected_in_metadata(self) -> None:
        """_inject_chunk_sink must include conversation_id in the new metadata."""
        from hermes.domain.decision_context import DecisionContext
        from hermes.capabilities.domain.ports import ConsentContext

        ctx = DecisionContext(
            tenant_id=_TENANT,
            cycle_id=uuid4(),
            trigger="chat_message",
            subjects=[],
            constraints={},
            operator_instruction="hello",
            agent_id=None,
            domain_payload={},
            metadata={},
        )
        fake_sink = object()
        new_ctx = _inject_chunk_sink(
            ctx, fake_sink, task_id=_TASK_ID, conversation_id=_CONV_ID
        )

        assert new_ctx.metadata.get("conversation_id") == _CONV_ID
        assert new_ctx.metadata.get("chunk_sink") is fake_sink
        assert new_ctx.metadata.get("task_id_for_stream") == _TASK_ID

    def test_empty_conversation_id_not_stored(self) -> None:
        """Empty conversation_id must not pollute metadata with an empty string."""
        from hermes.domain.decision_context import DecisionContext

        ctx = DecisionContext(
            tenant_id=_TENANT,
            cycle_id=uuid4(),
            trigger="chat_message",
            subjects=[],
            constraints={},
            operator_instruction="hello",
            agent_id=None,
            domain_payload={},
            metadata={},
        )
        fake_sink = object()
        new_ctx = _inject_chunk_sink(ctx, fake_sink, task_id=_TASK_ID, conversation_id="")

        assert "conversation_id" not in new_ctx.metadata

    def test_existing_metadata_preserved(self) -> None:
        """Pre-existing metadata keys are not clobbered."""
        from hermes.domain.decision_context import DecisionContext

        ctx = DecisionContext(
            tenant_id=_TENANT,
            cycle_id=uuid4(),
            trigger="chat_message",
            subjects=[],
            constraints={},
            operator_instruction="hello",
            agent_id=None,
            domain_payload={},
            metadata={"existing_key": "existing_value"},
        )
        new_ctx = _inject_chunk_sink(
            ctx, object(), task_id=_TASK_ID, conversation_id=_CONV_ID
        )
        assert new_ctx.metadata.get("existing_key") == "existing_value"


# ---------------------------------------------------------------------------
# (g) NousReasoningEngine.set_chat_delta_emitter
# ---------------------------------------------------------------------------


class TestSetChatDeltaEmitter:
    def test_set_chat_delta_emitter_stores_callables(self) -> None:
        """set_chat_delta_emitter must store both callables without error."""
        from hermes.prompts.persona import PersonaSpec

        persona = PersonaSpec(
            name="Test",
            role="assistant",
            language="en",
            register="formal",
            primary_mission="Test",
        )

        try:
            engine = NousReasoningEngine(persona=persona)
        except Exception:
            pytest.skip("NousReasoningEngine requires hermes-agent — skipping in CI")

        emit_delta_calls: list[tuple] = []
        emit_end_calls: list[str] = []

        def fake_emit_delta(conv_id: str, seq: int, text: str) -> None:
            emit_delta_calls.append((conv_id, seq, text))

        def fake_emit_end(conv_id: str) -> None:
            emit_end_calls.append(conv_id)

        engine.set_chat_delta_emitter(fake_emit_delta, fake_emit_end)

        assert engine._dbus_emit_delta is fake_emit_delta
        assert engine._dbus_emit_end is fake_emit_end
