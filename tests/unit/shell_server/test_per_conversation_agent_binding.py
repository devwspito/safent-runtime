"""Regression tests: per-conversation agent binding (one-conversation-one-agent contract).

Each chat is a contract with exactly one agent.  The contract is immutable
once a conversation is persisted: subsequent messages on that conversation
MUST reuse its bound agent regardless of whatever agent_id the caller passes.

Covers:
  (a) Two conversations bound to different agents each enqueue with their own
      target agent_id — no shared global bleeds between them.
  (b) A conversation with no prior agent_id resolves to DEFAULT_AGENT_ID.
  (c) An existing conversation keeps its bound agent even when the caller
      sends a different agent_id (contract immutability).

All three tests work at the application boundary (SQLiteConversationRepository +
ControlPlaneService with in-memory fakes), with no D-Bus or HTTP involved.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.agents.domain.agent import DEFAULT_AGENT_ID
from hermes.tasks.infrastructure.sqlite_conversation_repo import SQLiteConversationRepository
from hermes.tasks.control_plane.application.control_plane_service import (
    ControlPlaneService,
)
from hermes.tasks.control_plane.domain.ports import AuthenticatedChannel, EnqueueResult
from hermes.tasks.domain.ports import WorkItem, WorkItemKind

pytestmark = pytest.mark.unit

_TENANT = UUID("aa000000-0000-0000-0000-000000000001")
_OPERATOR_UID = 1000


# ---------------------------------------------------------------------------
# Fakes (minimal — only the surface ControlPlaneService needs)
# ---------------------------------------------------------------------------


class _InMemoryQueue:
    """Captures every enqueued WorkItem; returns a deterministic result."""

    def __init__(self) -> None:
        self.items: list[WorkItem] = []

    async def enqueue(self, item: WorkItem) -> WorkItem:
        self.items.append(item)
        return item

    async def pending_count(self) -> int:
        return len(self.items)


class _NullAgentState:
    async def pause(self, reason: str) -> None: ...
    async def resume(self) -> None: ...
    async def is_paused(self) -> bool: return False


def _make_service(queue: _InMemoryQueue) -> ControlPlaneService:
    return ControlPlaneService(
        tenant_id=_TENANT,
        queue=queue,
        agent_state=_NullAgentState(),
        authorized_uids=frozenset({_OPERATOR_UID}),
    )


def _channel() -> AuthenticatedChannel:
    return AuthenticatedChannel(sender_uid=_OPERATOR_UID)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> SQLiteConversationRepository:
    return SQLiteConversationRepository(db_path=tmp_path / "conv.db")


def _simulate_chat_start(
    *,
    repo: SQLiteConversationRepository,
    service: ControlPlaneService,
    conversation_id: UUID,
    user_message: str,
    requested_agent_id: str | None,
) -> tuple[str, EnqueueResult]:
    """Mirror of chat_start's 3-level agent resolution without HTTP overhead.

    Resolution order (matches main.py::chat_start):
      1. Bound agent from existing conversation (contract immutability).
      2. Caller-supplied agent_id (new conversation).
      3. DEFAULT_AGENT_ID (CEO).
    """
    bound_agent = repo.get_bound_agent_id(conversation_id=conversation_id)
    resolved_agent_id = bound_agent or requested_agent_id or DEFAULT_AGENT_ID

    repo.create_or_touch(
        conversation_id=conversation_id,
        first_user_message=user_message,
        agent_id=resolved_agent_id,
    )

    result = asyncio.run(
        service.enqueue(
            channel=_channel(),
            trigger_kind="chat_message",
            text=user_message,
            conversation_id=str(conversation_id),
            agent_id=resolved_agent_id,
        )
    )
    return resolved_agent_id, result


# ---------------------------------------------------------------------------
# (a) Two conversations → each carries its own agent_id, no shared global
# ---------------------------------------------------------------------------


class TestTwoConversationsBindToSeparateAgents:
    def test_each_enqueued_item_carries_own_agent_id(
        self, tmp_path: Path
    ) -> None:
        """Two conversations bound to different agents both enqueue with their
        own target agent_id; they do not share a global sentinel."""
        repo = _repo(tmp_path)
        queue = _InMemoryQueue()
        service = _make_service(queue)

        conv_design = uuid4()
        conv_sales = uuid4()

        _simulate_chat_start(
            repo=repo,
            service=service,
            conversation_id=conv_design,
            user_message="diseña el logo",
            requested_agent_id="agent-design",
        )
        _simulate_chat_start(
            repo=repo,
            service=service,
            conversation_id=conv_sales,
            user_message="prepara la oferta",
            requested_agent_id="agent-sales",
        )

        assert len(queue.items) == 2

        design_item = next(
            i for i in queue.items
            if i.payload.get("conversation_id") == str(conv_design)
        )
        sales_item = next(
            i for i in queue.items
            if i.payload.get("conversation_id") == str(conv_sales)
        )

        assert design_item.payload["agent_id"] == "agent-design"
        assert sales_item.payload["agent_id"] == "agent-sales"

        # They must be DIFFERENT — no global bleed.
        assert design_item.payload["agent_id"] != sales_item.payload["agent_id"]


# ---------------------------------------------------------------------------
# (b) Conversation with no agent_id → defaults to DEFAULT_AGENT_ID
# ---------------------------------------------------------------------------


class TestNoAgentIdDefaultsToCEO:
    def test_new_conversation_without_agent_id_resolves_to_default(
        self, tmp_path: Path
    ) -> None:
        """When neither the conversation nor the caller supplies an agent_id,
        resolution falls through to DEFAULT_AGENT_ID (CEO)."""
        repo = _repo(tmp_path)
        queue = _InMemoryQueue()
        service = _make_service(queue)

        conv_id = uuid4()
        resolved, _ = _simulate_chat_start(
            repo=repo,
            service=service,
            conversation_id=conv_id,
            user_message="hola",
            requested_agent_id=None,  # caller sends no agent_id
        )

        assert resolved == DEFAULT_AGENT_ID
        assert len(queue.items) == 1
        assert queue.items[0].payload["agent_id"] == DEFAULT_AGENT_ID

    def test_get_bound_agent_id_returns_none_for_unknown_conv(
        self, tmp_path: Path
    ) -> None:
        """get_bound_agent_id returns None for a conversation that has never
        been persisted — triggering fallback to caller / DEFAULT_AGENT_ID."""
        repo = _repo(tmp_path)
        result = repo.get_bound_agent_id(conversation_id=uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# (c) Existing conversation keeps its bound agent (contract immutability)
# ---------------------------------------------------------------------------


class TestContractImmutabilityOnSubsequentMessage:
    def test_second_message_keeps_original_agent_despite_different_request(
        self, tmp_path: Path
    ) -> None:
        """Even if a subsequent message arrives with a different agent_id,
        the bound agent from the first message wins (contract is immutable)."""
        repo = _repo(tmp_path)
        queue = _InMemoryQueue()
        service = _make_service(queue)

        conv_id = uuid4()

        # First message starts the conversation bound to agent-design.
        _simulate_chat_start(
            repo=repo,
            service=service,
            conversation_id=conv_id,
            user_message="primer mensaje",
            requested_agent_id="agent-design",
        )

        # Second message arrives asking for a different agent — must be ignored.
        resolved_second, _ = _simulate_chat_start(
            repo=repo,
            service=service,
            conversation_id=conv_id,
            user_message="segundo mensaje",
            requested_agent_id="agent-sales",  # attacker / UI bug / race
        )

        # The contract must survive.
        assert resolved_second == "agent-design"

        # Both WorkItems must carry the original binding.
        for item in queue.items:
            assert item.payload["agent_id"] == "agent-design", (
                f"WorkItem '{item.id}' carried '{item.payload.get('agent_id')}' "
                "instead of the originally bound 'agent-design'. "
                "The one-conversation-one-agent contract was violated."
            )

    def test_bound_agent_persisted_in_repo_after_first_message(
        self, tmp_path: Path
    ) -> None:
        """After the first message, the repository must return the bound agent
        on subsequent calls to get_bound_agent_id."""
        repo = _repo(tmp_path)
        queue = _InMemoryQueue()
        service = _make_service(queue)

        conv_id = uuid4()
        _simulate_chat_start(
            repo=repo,
            service=service,
            conversation_id=conv_id,
            user_message="hola",
            requested_agent_id="agent-design",
        )

        # The repo must surface the bound agent for future lookups.
        bound = repo.get_bound_agent_id(conversation_id=conv_id)
        assert bound == "agent-design"

    def test_null_agent_id_in_existing_conv_resolves_to_default_then_stays(
        self, tmp_path: Path
    ) -> None:
        """Old conversations with NULL agent_id: the first new message resolves
        them to DEFAULT_AGENT_ID and persists it; subsequent messages see CEO."""
        repo = _repo(tmp_path)
        queue = _InMemoryQueue()
        service = _make_service(queue)

        conv_id = uuid4()

        # Simulate a legacy row with agent_id = NULL (pre-migration conversation).
        # create_or_touch with agent_id=None produces this state.
        repo.create_or_touch(
            conversation_id=conv_id,
            first_user_message="legacy message",
            agent_id=None,
        )

        # get_bound_agent_id returns None for legacy rows.
        assert repo.get_bound_agent_id(conversation_id=conv_id) is None

        # chat_start resolution: NULL → caller=None → DEFAULT_AGENT_ID
        resolved, _ = _simulate_chat_start(
            repo=repo,
            service=service,
            conversation_id=conv_id,
            user_message="new message on old conv",
            requested_agent_id=None,
        )

        # After resolution the conversation is NOT overwritten (create_or_touch
        # is idempotent for existing rows), but the WorkItem carries the default.
        assert resolved == DEFAULT_AGENT_ID
        assert queue.items[-1].payload["agent_id"] == DEFAULT_AGENT_ID
