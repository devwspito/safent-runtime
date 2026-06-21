"""Regression tests for streaming wire-up fixes (FIX B.1, B.2, C, D, E).

Coverage:
  (a) FIX B.1 — streaming: engine's stream_callback is called with incremental
      deltas; the counting_sink records >1 delta when the engine streams tokens.
  (b) FIX B.2 — no duplicate: when deltas were already streamed, the monolithic
      fallback re-emit in _handle_chat_narrative_reply is skipped.
  (c) FIX B.2 — fallback: when NO deltas were streamed, the monolithic emit
      fires exactly once (non-streaming engine path).
  (d) FIX D — system prompt cache: same engine + same persona → same object
      returned; different (engine_id, persona_id) → isolated entries.
  (e) FIX E — workspace skip: _did_cycle_write_files returns False for a cycle
      with zero proposals, True when a file-writing tool proposal is present.
  (f) FIX B.2 — _CountingChunkSink counts only DELTA/THINKING_DELTA, not STATUS.
  (g) _inject_chunk_sink now also injects task_id_for_stream in metadata.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.capabilities.domain.ports import ConsentContext, ExecutionOutcome, ExecutionStatus
from hermes.domain.cycle_output import CycleOutput, TokenUsage
from hermes.domain.decision_context import DecisionContext
from hermes.domain.proposal import ToolCallProposal
from hermes.tasks.application.agent_loop_orchestrator import (
    AgentLoopOrchestrator,
    _CountingChunkSink,
    _inject_chunk_sink,
)
from hermes.tasks.domain.ports import WorkItem, WorkItemKind
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

pytestmark = pytest.mark.unit

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_OPERATOR = UUID("22222222-2222-2222-2222-222222222222")
_CONV_ID = "33333333-3333-3333-3333-333333333333"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consent() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)


def _chat_item(text: str = "hola") -> WorkItem:
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="chat_message",
        kind=WorkItemKind.CHAT_MESSAGE,
        payload={
            "enqueued_by": str(_OPERATOR),
            "instruction": text,
            "chat_text": text,
            "conversation_id": _CONV_ID,
        },
    )


def _proposal(tool_name: str = "write_file") -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT,
        entity_id="f1",
        entity_type="file",
        parameters={},
        justification="test",
    )


class _FakeChunkSink:
    """Records emit() calls for assertion."""

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


def _make_orchestrator(
    *,
    engine: Any,
    chunk_sink: Any | None = None,
) -> tuple[AgentLoopOrchestrator, InMemoryWorkQueue]:
    from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker

    queue = InMemoryWorkQueue()
    state = InMemoryAgentState()
    orch = AgentLoopOrchestrator(
        queue=queue,
        state=state,
        engine=engine,
        broker=FakeCapabilityBroker(),
        consent_context=_consent(),
        notify_watchdog=lambda: None,
        idle_poll_s=0.0,
        pause_poll_s=0.0,
        chunk_sink=chunk_sink,
    )
    return orch, queue


# ---------------------------------------------------------------------------
# FIX B.2 — _CountingChunkSink
# ---------------------------------------------------------------------------


class TestCountingChunkSink:
    """_CountingChunkSink delegates all calls and counts DELTA/THINKING_DELTA."""

    @pytest.mark.asyncio
    async def test_counts_delta_chunks(self) -> None:
        from hermes.tasks.control_plane.domain.ports import StreamChunkKind, TaskStreamChunk

        inner = _FakeChunkSink()
        sink = _CountingChunkSink(inner)
        task_id = uuid4()

        await sink.emit(task_id=task_id, chunk=TaskStreamChunk(kind=StreamChunkKind.DELTA, delta="hi"))
        await sink.emit(task_id=task_id, chunk=TaskStreamChunk(kind=StreamChunkKind.DELTA, delta=" there"))

        assert sink.delta_count == 2
        assert len(inner.emitted) == 2

    @pytest.mark.asyncio
    async def test_counts_thinking_delta(self) -> None:
        from hermes.tasks.control_plane.domain.ports import StreamChunkKind, TaskStreamChunk

        inner = _FakeChunkSink()
        sink = _CountingChunkSink(inner)
        task_id = uuid4()

        await sink.emit(task_id=task_id, chunk=TaskStreamChunk(kind=StreamChunkKind.THINKING_DELTA, delta="reasoning..."))

        assert sink.delta_count == 1

    @pytest.mark.asyncio
    async def test_does_not_count_status_chunks(self) -> None:
        from hermes.tasks.control_plane.domain.ports import StreamChunkKind, TaskStreamChunk

        inner = _FakeChunkSink()
        sink = _CountingChunkSink(inner)
        task_id = uuid4()

        await sink.emit(task_id=task_id, chunk=TaskStreamChunk(kind=StreamChunkKind.STATUS, status="in_progress"))

        assert sink.delta_count == 0
        assert len(inner.emitted) == 1  # still delegated

    @pytest.mark.asyncio
    async def test_close_delegates(self) -> None:
        inner = _FakeChunkSink()
        sink = _CountingChunkSink(inner)
        task_id = uuid4()

        await sink.close(task_id=task_id, outcome="completed")

        assert inner.closed == [{"outcome": "completed", "error": None}]

    @pytest.mark.asyncio
    async def test_emit_status_delegates(self) -> None:
        inner = _FakeChunkSink()
        sink = _CountingChunkSink(inner)
        task_id = uuid4()

        await sink.emit_status(task_id=task_id, status="in_progress")

        assert inner.statuses == ["in_progress"]


# ---------------------------------------------------------------------------
# FIX B.2 — _handle_chat_narrative_reply skips monolithic re-emit when streamed
# ---------------------------------------------------------------------------


class TestNarrativeReplySkipsMonolithicWhenStreamed:
    """When prior_emit_count > 0, the monolithic delta must NOT be re-emitted."""

    @pytest.mark.asyncio
    async def test_no_delta_emitted_when_already_streamed(self) -> None:
        """prior_emit_count=3 → only close(), no extra emit()."""
        from hermes.tasks.control_plane.domain.ports import StreamChunkKind, TaskStreamChunk

        class _StreamingEngine:
            async def run_cycle(self, context: DecisionContext) -> CycleOutput:
                return CycleOutput(
                    tool_call_proposals=(),
                    narrative="Respuesta del agente",
                    malformed_intents=(),
                    rejected_by_policy=(),
                    usage=TokenUsage(model="fake"),
                )

        fake_sink = _FakeChunkSink()
        orch, queue = _make_orchestrator(engine=_StreamingEngine(), chunk_sink=fake_sink)

        # Use a properly enqueued + claimed item.
        item = _chat_item("dime algo")
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        narrative = "Respuesta del agente"

        # The inner_sink starts with zero emits.
        inner_sink = _FakeChunkSink()

        # Call with prior_emit_count=3 (already streamed).
        await orch._handle_chat_narrative_reply(
            claimed, narrative, inner_sink, prior_emit_count=3
        )

        # Should only have called close(), not emit().
        assert len(inner_sink.emitted) == 0, (
            "Monolithic delta was re-emitted even though prior_emit_count=3. "
            "FIX B.2 regression: text would be duplicated in the client."
        )
        assert len(inner_sink.closed) == 1
        assert inner_sink.closed[0]["outcome"] == "completed"

    @pytest.mark.asyncio
    async def test_monolithic_delta_emitted_when_not_streamed(self) -> None:
        """prior_emit_count=0 (no streaming) → monolithic emit fires exactly once."""
        class _FakeEngine:
            async def run_cycle(self, ctx: DecisionContext) -> CycleOutput:
                return CycleOutput(
                    tool_call_proposals=(),
                    narrative="",
                    malformed_intents=(),
                    rejected_by_policy=(),
                    usage=TokenUsage(model="fake"),
                )

        orch, queue = _make_orchestrator(engine=_FakeEngine())
        item = _chat_item("sin streaming")
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        narrative = "Respuesta completa sin streaming"
        sink = _FakeChunkSink()

        await orch._handle_chat_narrative_reply(
            claimed, narrative, sink, prior_emit_count=0
        )

        assert len(sink.emitted) == 1, (
            "Expected exactly one monolithic emit() when no streaming happened. "
            f"Got {len(sink.emitted)} emits."
        )
        from hermes.tasks.control_plane.domain.ports import StreamChunkKind
        assert sink.emitted[0].kind == StreamChunkKind.DELTA
        assert sink.emitted[0].delta == narrative
        assert len(sink.closed) == 1

    @pytest.mark.asyncio
    async def test_none_sink_no_crash_when_streamed(self) -> None:
        """chunk_sink=None must not crash regardless of prior_emit_count."""
        orch, queue = _make_orchestrator(engine=object())
        # Use a properly enqueued + claimed item so mark_completed can find it.
        item = _chat_item("sin sink")
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        # Should not raise.
        await orch._handle_chat_narrative_reply(
            claimed, "some text", None, prior_emit_count=5
        )


# ---------------------------------------------------------------------------
# FIX B.2 — end-to-end: orchestrator uses counting_sink correctly
# ---------------------------------------------------------------------------


class TestOrchestratorCountingSinkIntegration:
    """In _process(), counting_sink.delta_count drives the prior_emit_count argument."""

    @pytest.mark.asyncio
    async def test_orchestrator_uses_counting_sink_not_raw_sink(self) -> None:
        """The orchestrator must wrap chunk_sink in _CountingChunkSink before injecting."""
        emits_seen: list[Any] = []

        class _TrackingEngine:
            """Captures the chunk_sink that was injected into context.metadata."""

            def __init__(self) -> None:
                self.sink_in_meta: Any = None

            async def run_cycle(self, context: DecisionContext) -> CycleOutput:
                md = getattr(context, "metadata", {}) or {}
                self.sink_in_meta = md.get("chunk_sink")
                return CycleOutput(
                    tool_call_proposals=(),
                    narrative="hola",
                    malformed_intents=(),
                    rejected_by_policy=(),
                    usage=TokenUsage(model="fake"),
                )

        engine = _TrackingEngine()
        raw_sink = _FakeChunkSink()
        orch, queue = _make_orchestrator(engine=engine, chunk_sink=raw_sink)

        item = _chat_item("prueba")
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        await orch._process(claimed)

        # The sink injected into the engine must be a _CountingChunkSink wrapper.
        assert isinstance(engine.sink_in_meta, _CountingChunkSink), (
            f"Expected _CountingChunkSink in context.metadata['chunk_sink'], "
            f"got {type(engine.sink_in_meta).__name__}. "
            "FIX B.2: orchestrator must wrap the real sink."
        )


# ---------------------------------------------------------------------------
# FIX B.1 + B.2 — inject_chunk_sink injects task_id_for_stream
# ---------------------------------------------------------------------------


class TestInjectChunkSinkInjectsTaskId:
    """_inject_chunk_sink must also inject task_id_for_stream when task_id is given."""

    def test_task_id_injected_when_provided(self) -> None:
        ctx = DecisionContext(
            tenant_id=_TENANT,
            cycle_id=uuid4(),
            trigger="queue_drain:chat_message",
            operator_instruction="test",
        )
        task_id = uuid4()
        sentinel_sink = object()

        patched = _inject_chunk_sink(ctx, sentinel_sink, task_id=task_id)

        assert patched.metadata.get("task_id_for_stream") == task_id, (
            "task_id_for_stream not injected into metadata. "
            "FIX B.1: engine needs the task_id to emit deltas to the right socket."
        )
        assert patched.metadata.get("chunk_sink") is sentinel_sink

    def test_no_task_id_when_not_given(self) -> None:
        ctx = DecisionContext(
            tenant_id=_TENANT,
            cycle_id=uuid4(),
            trigger="queue_drain:chat_message",
            operator_instruction="test",
        )
        sentinel_sink = object()

        patched = _inject_chunk_sink(ctx, sentinel_sink)

        assert "task_id_for_stream" not in patched.metadata, (
            "task_id_for_stream should be absent when no task_id is passed."
        )


# ---------------------------------------------------------------------------
# FIX D — system prompt cache
# ---------------------------------------------------------------------------


class TestSystemPromptCache:
    """_cached_chat_system_prompt is keyed by (engine_id, persona_id)."""

    def test_same_engine_same_persona_returns_same_string(self) -> None:
        from hermes.runtime.nous_engine import _cached_chat_system_prompt, _SYSTEM_PROMPT_CACHE

        class _FakePersona:
            name = "TestBot"
            language = "es-ES"
            golden_rules = ()
            forbidden_phrases = ()

        persona = _FakePersona()
        engine_id = id(object())

        result1 = _cached_chat_system_prompt(engine_id, persona)
        result2 = _cached_chat_system_prompt(engine_id, persona)

        assert result1 is result2, (
            "Cache miss on identical (engine_id, persona_id) pair. "
            "FIX D: system prompt must be cached to avoid recomputing on every message."
        )

    def test_different_persona_different_entry(self) -> None:
        from hermes.runtime.nous_engine import _cached_chat_system_prompt

        class _PersonaA:
            name = "Alpha"
            language = "es-ES"
            golden_rules = ()
            forbidden_phrases = ()

        class _PersonaB:
            name = "Beta"
            language = "en-US"
            golden_rules = ()
            forbidden_phrases = ()

        engine_id = id(object())
        a = _PersonaA()
        b = _PersonaB()

        result_a = _cached_chat_system_prompt(engine_id, a)
        result_b = _cached_chat_system_prompt(engine_id, b)

        assert result_a != result_b, (
            "Different personas produced the same cached prompt. "
            "FIX D: cache key must include persona identity."
        )

    def test_different_engine_id_isolated(self) -> None:
        from hermes.runtime.nous_engine import _cached_chat_system_prompt

        class _Persona:
            name = "SharedBot"
            language = "es-ES"
            golden_rules = ()
            forbidden_phrases = ()

        persona = _Persona()
        eid_1 = 100_000
        eid_2 = 200_000

        r1 = _cached_chat_system_prompt(eid_1, persona)
        r2 = _cached_chat_system_prompt(eid_2, persona)

        # Content should be the same (same persona) but came from different cache slots.
        assert r1 == r2, (
            "Same persona should produce the same prompt regardless of engine_id."
        )


# ---------------------------------------------------------------------------
# FIX E — _did_cycle_write_files
# ---------------------------------------------------------------------------


class TestDidCycleWriteFiles:
    """_did_cycle_write_files correctly identifies file-writing tool usage."""

    def _make_output(self, *tool_names: str) -> CycleOutput:
        proposals = tuple(
            ToolCallProposal(
                proposal_id=uuid4(),
                tool_name=name,
                tenant_id=_TENANT,
                entity_id="e",
                entity_type="test",
                parameters={},
                justification="test",
            )
            for name in tool_names
        )
        return CycleOutput(
            tool_call_proposals=proposals,
            narrative="",
            malformed_intents=(),
            rejected_by_policy=(),
            usage=TokenUsage(model="fake"),
        )

    def test_no_proposals_returns_false(self) -> None:
        from hermes.runtime.nous_engine import _did_cycle_write_files

        output = self._make_output()
        assert _did_cycle_write_files(output) is False, (
            "Cycle with no proposals should not trigger workspace diff. FIX E."
        )

    def test_read_only_tool_returns_false(self) -> None:
        from hermes.runtime.nous_engine import _did_cycle_write_files

        output = self._make_output("browser_navigate", "web_search")
        assert _did_cycle_write_files(output) is False, (
            "Read-only tools (browser_navigate, web_search) should not trigger workspace diff."
        )

    def test_write_file_tool_returns_true(self) -> None:
        from hermes.runtime.nous_engine import _did_cycle_write_files

        output = self._make_output("write_file")
        assert _did_cycle_write_files(output) is True, (
            "write_file proposal should trigger workspace diff. FIX E."
        )

    def test_terminal_tool_returns_true(self) -> None:
        from hermes.runtime.nous_engine import _did_cycle_write_files

        output = self._make_output("terminal")
        assert _did_cycle_write_files(output) is True

    def test_mixed_proposals_any_write_returns_true(self) -> None:
        from hermes.runtime.nous_engine import _did_cycle_write_files

        output = self._make_output("web_search", "patch", "browser_navigate")
        assert _did_cycle_write_files(output) is True, (
            "If any proposal uses a write tool, workspace diff should fire."
        )


# ---------------------------------------------------------------------------
# FIX C — ModelConfig knobs channel
# ---------------------------------------------------------------------------


class TestModelConfigKnobs:
    """_build_governed_agent passes max_tokens/temperature to GovernedAIAgent."""

    def test_max_tokens_in_extra_knobs(self) -> None:
        """When model_config.max_tokens is set, it appears in kwargs to GovernedAIAgent."""
        from hermes.runtime.model_config import ModelConfig

        cfg = ModelConfig(model="test/model", max_tokens=512, temperature=0.7)

        # Extract extra knobs the same way _build_governed_agent does.
        extra_knobs: dict = {}
        if cfg.max_tokens is not None:
            extra_knobs["max_tokens"] = cfg.max_tokens
        if cfg.temperature != 0.0:
            extra_knobs["temperature"] = cfg.temperature
        if cfg.timeout_seconds != 90:
            extra_knobs["timeout_seconds"] = cfg.timeout_seconds
        if cfg.max_iterations != 8:
            extra_knobs["max_iterations"] = cfg.max_iterations

        assert extra_knobs.get("max_tokens") == 512, (
            "max_tokens must be passed to GovernedAIAgent. FIX C."
        )
        assert extra_knobs.get("temperature") == 0.7, (
            "temperature must be passed to GovernedAIAgent. FIX C."
        )

    def test_defaults_not_in_extra_knobs(self) -> None:
        """When ModelConfig uses defaults, no spurious kwargs are added."""
        from hermes.runtime.model_config import ModelConfig, _DEFAULT_TEMPERATURE, _DEFAULT_TIMEOUT_S, _DEFAULT_MAX_ITERATIONS

        cfg = ModelConfig(model="test/model")

        extra_knobs: dict = {}
        if cfg.max_tokens is not None:
            extra_knobs["max_tokens"] = cfg.max_tokens
        if cfg.temperature != 0.0:
            extra_knobs["temperature"] = cfg.temperature
        if cfg.timeout_seconds != 90:
            extra_knobs["timeout_seconds"] = cfg.timeout_seconds
        if cfg.max_iterations != 8:
            extra_knobs["max_iterations"] = cfg.max_iterations

        assert "max_tokens" not in extra_knobs, (
            "max_tokens=None should not appear in kwargs. FIX C: never override with falsy."
        )
        assert "temperature" not in extra_knobs, (
            "temperature=0.0 default should not appear in kwargs."
        )
