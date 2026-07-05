"""Regression tests for two chat pipeline bugs (spec 014 / Bug #1 + #2).

Bug #1 — operator_instruction silently dropped by _inject_chunk_sink
    Root cause: _inject_chunk_sink rebuilt DecisionContext without forwarding
    operator_instruction and agent_id, resetting them to "" / None.  Every
    chat_message goes through that function, so the engine always received an
    empty instruction and fell through to the "Hola" fallback.

Bug #2 — assistant reply not persisted in conversation store
    Root cause: _handle_chat_narrative_reply only emitted to the stream socket
    and marked the task completed, but never called conversation_repo.append_message
    for the assistant turn.  GetConversation then returned only the user message.

Each test MUST FAIL before the fix and PASS after.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from hermes.capabilities.domain.ports import ConsentContext, ExecutionOutcome, ExecutionStatus
from hermes.domain.decision_context import DecisionContext
from hermes.domain.cycle_output import CycleOutput, TokenUsage
from hermes.tasks.application.agent_loop_orchestrator import (
    AgentLoopOrchestrator,
    _inject_chunk_sink,
)
from hermes.tasks.domain.ports import WorkItem, WorkItemKind
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

pytestmark = pytest.mark.unit

_TENANT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_OPERATOR = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_CONV_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consent() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)


def _chat_item(text: str, conv_id: str = _CONV_ID) -> WorkItem:
    """WorkItem of kind CHAT_MESSAGE with the given user text."""
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="chat_message",
        kind=WorkItemKind.CHAT_MESSAGE,
        payload={
            "enqueued_by": str(_OPERATOR),
            "instruction": text,
            "chat_text": text,
            "conversation_id": conv_id,
        },
    )


class _RecordingEngine:
    """Fake engine that records the DecisionContext it receives and returns a narrative."""

    def __init__(self, narrative: str = "Soy Safent, tu asistente.") -> None:
        self.received: list[DecisionContext] = []
        self._narrative = narrative

    async def run_cycle(self, context: DecisionContext) -> CycleOutput:
        self.received.append(context)
        return CycleOutput(
            tool_call_proposals=(),
            narrative=self._narrative,
            malformed_intents=(),
            rejected_by_policy=(),
            usage=TokenUsage(model="fake"),
        )


class _InMemoryConversationRepo:
    """Minimal in-memory conversation store that mimics the production interface."""

    def __init__(self) -> None:
        self._messages: list[dict] = []

    def create_or_touch(self, *, conversation_id, first_user_message, agent_id=None):
        pass  # not needed for these tests

    def append_message(self, *, conversation_id, role: str, content: str) -> None:
        self._messages.append(
            {"conversation_id": str(conversation_id), "role": role, "content": content}
        )

    def messages_for(self, conversation_id: str) -> list[dict]:
        return [m for m in self._messages if m["conversation_id"] == conversation_id]


class _FakeBroker:
    """Broker that never dispatches (chat_message produces a narrative, no proposals)."""

    async def dispatch(self, proposal, consent_context, **kwargs):
        # Should not be called for a pure-narrative chat reply.
        raise AssertionError("dispatch should not be called for a narrative-only reply")


def _make_orchestrator(
    *,
    engine: _RecordingEngine,
    conversation_repo: _InMemoryConversationRepo | None = None,
) -> tuple[AgentLoopOrchestrator, InMemoryWorkQueue]:
    from hermes.capabilities.testing.fake_capability_broker import FakeCapabilityBroker

    queue = InMemoryWorkQueue()
    state = InMemoryAgentState()
    return (
        AgentLoopOrchestrator(
            queue=queue,
            state=state,
            engine=engine,
            broker=FakeCapabilityBroker(),
            consent_context=_consent(),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
            conversation_repo=conversation_repo,
        ),
        queue,
    )


# ---------------------------------------------------------------------------
# Bug #1 — operator_instruction must survive _inject_chunk_sink
# ---------------------------------------------------------------------------


class TestBug1OperatorInstructionPreserved:
    """_inject_chunk_sink must forward operator_instruction (and agent_id) unchanged."""

    def test_inject_chunk_sink_preserves_operator_instruction(self) -> None:
        """Before fix: operator_instruction was reset to '' by _inject_chunk_sink.

        This is the unit-level regression: build a DecisionContext with a non-empty
        operator_instruction, inject a chunk_sink, and verify the field survives.
        """
        user_text = "En una frase corta, ¿que eres?"
        ctx = DecisionContext(
            tenant_id=_TENANT,
            cycle_id=uuid4(),
            trigger="queue_drain:chat_message",
            operator_instruction=user_text,
            agent_id="agent-safent",
            domain_payload={"conversation_id": _CONV_ID},
        )

        sentinel_sink = object()  # any non-None object
        patched = _inject_chunk_sink(ctx, sentinel_sink)

        assert patched.operator_instruction == user_text, (
            f"operator_instruction was '{patched.operator_instruction}' after "
            f"_inject_chunk_sink but should be '{user_text}'. "
            "Bug #1: field was dropped when rebuilding DecisionContext."
        )
        assert patched.agent_id == "agent-safent", (
            f"agent_id was '{patched.agent_id}' after _inject_chunk_sink, expected 'agent-safent'."
        )
        assert patched.metadata.get("chunk_sink") is sentinel_sink, (
            "chunk_sink must be present in patched metadata."
        )

    async def test_engine_receives_real_text_not_hola(self) -> None:
        """End-to-end: the engine must receive the actual user text, not the 'Hola' fallback.

        Before fix: _inject_chunk_sink reset operator_instruction to '', so the
        engine received '' → _chat_user fell through to "Hola".
        After fix: operator_instruction propagates and the engine receives the text.
        """
        user_text = "En una frase corta, ¿que eres?"
        engine = _RecordingEngine()

        class _FakeChunkSink:
            async def emit_status(self, **kwargs): pass
            async def emit(self, **kwargs): pass
            async def close(self, **kwargs): pass

        orch, queue = _make_orchestrator(engine=engine)
        # Inject a fake chunk_sink so _inject_chunk_sink is triggered
        orch._chunk_sink = _FakeChunkSink()

        item = _chat_item(user_text)
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        await orch._process(claimed)

        assert len(engine.received) == 1, "engine.run_cycle must be called exactly once"
        ctx = engine.received[0]
        assert ctx.operator_instruction == user_text, (
            f"Engine received operator_instruction='{ctx.operator_instruction}' "
            f"but expected '{user_text}'. "
            "Bug #1: _inject_chunk_sink dropped operator_instruction."
        )


# ---------------------------------------------------------------------------
# Bug #2 — assistant reply must be persisted in conversation_repo
# ---------------------------------------------------------------------------


class TestBug2AssistantReplyPersisted:
    """After a chat_message cycle, the assistant reply must appear in conversation_repo."""

    async def test_assistant_message_persisted_after_chat_cycle(self) -> None:
        """Before fix: conversation_repo.append_message was never called for 'assistant'.

        After fix: _handle_chat_narrative_reply calls append_message(role='assistant').
        """
        agent_reply = "Soy Safent, tu asistente personal."
        engine = _RecordingEngine(narrative=agent_reply)
        conv_repo = _InMemoryConversationRepo()

        orch, queue = _make_orchestrator(engine=engine, conversation_repo=conv_repo)

        item = _chat_item("En una frase corta, ¿que eres?")
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        await orch._process(claimed)

        messages = conv_repo.messages_for(_CONV_ID)
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1, (
            f"Expected 1 assistant message in conversation_repo for {_CONV_ID!r}, "
            f"got {len(assistant_msgs)}. "
            "Bug #2: _handle_chat_narrative_reply never called append_message."
        )
        assert assistant_msgs[0]["content"] == agent_reply, (
            f"assistant message content is '{assistant_msgs[0]['content']}', "
            f"expected '{agent_reply}'."
        )

    async def test_no_crash_when_conversation_repo_is_none(self) -> None:
        """Orchestrator must not crash if conversation_repo is not injected (best-effort)."""
        engine = _RecordingEngine(narrative="Respuesta sin persistencia.")

        orch, queue = _make_orchestrator(engine=engine, conversation_repo=None)

        item = _chat_item("¿Que puedes hacer?")
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        # Must complete without raising even though conversation_repo is None.
        await orch._process(claimed)
        assert len(engine.received) == 1, "engine.run_cycle must still be called once"

    async def test_assistant_reply_not_persisted_without_conversation_id(self) -> None:
        """If the item has no conversation_id, append_message is not called (no crash)."""
        engine = _RecordingEngine(narrative="Sin conversation_id.")
        conv_repo = _InMemoryConversationRepo()

        orch, queue = _make_orchestrator(engine=engine, conversation_repo=conv_repo)

        # Build an item WITHOUT conversation_id in payload
        item = WorkItem.new(
            tenant_id=_TENANT,
            trigger_kind="chat_message",
            kind=WorkItemKind.CHAT_MESSAGE,
            payload={
                "enqueued_by": str(_OPERATOR),
                "instruction": "Sin conversation_id",
                "chat_text": "Sin conversation_id",
                # intentionally no "conversation_id" key
            },
        )
        await queue.enqueue(item)
        claimed = await queue.claim_next()
        assert claimed is not None

        await orch._process(claimed)  # must not raise

        # No messages persisted since there is no valid conversation_id
        assert conv_repo._messages == [], (
            "No messages should be appended when conversation_id is missing."
        )
