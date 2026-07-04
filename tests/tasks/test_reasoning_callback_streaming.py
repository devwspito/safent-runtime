"""Regression: reasoning/CoT tokens never streamed to the client (chat-freeze bug).

Root cause: hermes-agent (Nous v0.15.1) routes reasoning/thinking text through the
AIAgent CONSTRUCTOR-time ``reasoning_callback`` kwarg (see
``agent._fire_reasoning_delta`` / ``chat_completion_helpers.py``:
``delta.reasoning_content -> agent._fire_reasoning_delta(text)`` ->
``agent.reasoning_callback(text)``). It is NEVER routed through
``run_conversation(stream_callback=...)`` — the callback nous_engine.py's
``_build_governed_agent`` builds and wires. Before this fix, ``reasoning_callback``
was never passed to ``GovernedAIAgent``/``AIAgent`` at all, so the THINKING_DELTA
branch in ``_build_stream_callback`` was dead code for any provider that streams
structured reasoning (Qwen3.x, DeepSeek-R1, etc. over an OpenAI-compatible
endpoint) — the model's chain-of-thought silently vanished instead of streaming
to the SSE client.

Coverage:
  (a) A callback shaped like ``lambda text: stream_cb(text, "thinking_delta")``
      (the exact pattern wired in run_cycle) produces a THINKING_DELTA
      TaskStreamChunk via chunk_sink.emit — proves the KIND dispatch is correct.
  (b) ``_build_governed_agent`` forwards its ``reasoning_callback`` parameter into
      the constructed agent (AIAgent constructor kwargs) — proves the WIRING that
      was previously entirely missing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from hermes.runtime.model_config import ModelConfig
from hermes.runtime.nous_engine import (
    NousReasoningEngine,
    _build_stream_callback,
)
from hermes.prompts.persona import PersonaSpec
from hermes.tasks.control_plane.domain.ports import StreamChunkKind, TaskStreamChunk

pytestmark = pytest.mark.unit

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_TASK_ID = UUID("22222222-2222-2222-2222-222222222222")


def _persona() -> PersonaSpec:
    return PersonaSpec(
        name="Lumen",
        role="asistente",
        language="es-ES",
        register="",
        primary_mission="ayudar",
    )


class TestReasoningCallbackKindDispatch:
    """(a) The wrapper pattern used in run_cycle emits a THINKING_DELTA chunk."""

    def test_reasoning_wrapper_emits_thinking_delta_chunk(self) -> None:
        chunk_sink = MagicMock()
        loop = asyncio.new_event_loop()
        stream_cb = _build_stream_callback(chunk_sink, _TASK_ID, loop, [0])

        # Exact pattern nous_engine.py's run_cycle wires as `reasoning_callback`.
        reasoning_cb = lambda text: stream_cb(text, "thinking_delta")  # noqa: E731

        reasoning_cb("estoy pensando en la respuesta")

        assert chunk_sink.emit.called, "chunk_sink.emit must be called for reasoning text"
        call_kwargs = chunk_sink.emit.call_args.kwargs
        chunk = call_kwargs["chunk"]
        assert chunk == TaskStreamChunk(
            kind=StreamChunkKind.THINKING_DELTA, delta="estoy pensando en la respuesta"
        )

    def test_plain_delta_still_dispatches_as_delta(self) -> None:
        """Sanity: the SAME callback with no kind arg still emits a plain DELTA
        (regression guard against accidentally flipping the default)."""
        chunk_sink = MagicMock()
        loop = asyncio.new_event_loop()
        stream_cb = _build_stream_callback(chunk_sink, _TASK_ID, loop, [0])

        stream_cb("hola")

        chunk = chunk_sink.emit.call_args.kwargs["chunk"]
        assert chunk == TaskStreamChunk(kind=StreamChunkKind.DELTA, delta="hola")


class TestBuildGovernedAgentWiresReasoningCallback:
    """(b) _build_governed_agent must forward reasoning_callback to AIAgent.

    Before the fix, `_build_governed_agent` had no `reasoning_callback` parameter
    at all and never passed one to `GovernedAIAgent(...)` — this test fails on
    the pre-fix code (TypeError: unexpected keyword argument, or the captured
    kwargs simply lack the key) and passes after.
    """

    def test_reasoning_callback_reaches_ai_agent_constructor(self) -> None:
        captured_kwargs: dict = {}

        def fake_ai_agent_cls(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        engine = NousReasoningEngine(persona=_persona())

        def my_reasoning_cb(text: str) -> None:
            pass

        with (
            patch("hermes.runtime.nous_engine._import_ai_agent", return_value=fake_ai_agent_cls),
            patch(
                "hermes.runtime.nous_engine._cached_resolve_hermes_runtime",
                return_value=(
                    {
                        "api_key": None,
                        "base_url": None,
                        "provider": "openai-api",
                        "api_mode": "chat_completions",
                        "credential_pool": None,
                    },
                    "test-model",
                ),
            ),
            patch("hermes.runtime.nous_engine._cached_enrich_prompt", side_effect=lambda p, t: p),
        ):
            engine._build_governed_agent(
                ModelConfig(model="test/model"),
                "system prompt",
                asyncio.new_event_loop(),
                _TENANT,
                None,
                reasoning_callback=my_reasoning_cb,
            )

        assert captured_kwargs.get("reasoning_callback") is my_reasoning_cb

    def test_reasoning_callback_defaults_to_none(self) -> None:
        """No stream_cb wired (e.g. non-chat cycle) -> reasoning_callback=None,
        never crashes AIAgent construction."""
        captured_kwargs: dict = {}

        def fake_ai_agent_cls(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        engine = NousReasoningEngine(persona=_persona())

        with (
            patch("hermes.runtime.nous_engine._import_ai_agent", return_value=fake_ai_agent_cls),
            patch(
                "hermes.runtime.nous_engine._cached_resolve_hermes_runtime",
                return_value=(
                    {
                        "api_key": None,
                        "base_url": None,
                        "provider": "openai-api",
                        "api_mode": "chat_completions",
                        "credential_pool": None,
                    },
                    "test-model",
                ),
            ),
            patch("hermes.runtime.nous_engine._cached_enrich_prompt", side_effect=lambda p, t: p),
        ):
            engine._build_governed_agent(
                ModelConfig(model="test/model"),
                "system prompt",
                asyncio.new_event_loop(),
                _TENANT,
                None,
            )

        assert captured_kwargs.get("reasoning_callback") is None
