"""Archivar, restaurar y borrar conversaciones desde «Recientes» (14-sep-2026)."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from hermes.tasks.infrastructure.sqlite_conversation_repo import (
    ConversationNotFound,
    SQLiteConversationRepository,
)


def _repo(tmp_path: Path) -> SQLiteConversationRepository:
    repo = SQLiteConversationRepository(db_path=tmp_path / "shell-state.db")
    for text in ("hola A", "hola B"):
        repo.create_or_touch(conversation_id=uuid4(), first_user_message=text, agent_id="default")
    return repo


def test_archived_conversations_leave_recents_and_come_back_on_unarchive(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = repo.list_summaries()[0].conversation_id
    repo.archive(conversation_id=target)
    assert [c.conversation_id for c in repo.list_summaries()] != [target]
    assert target not in {c.conversation_id for c in repo.list_summaries()}
    everything = repo.list_summaries(include_archived=True)
    archived = [c for c in everything if c.conversation_id == target]
    assert archived and archived[0].archived is True
    repo.unarchive(conversation_id=target)
    assert target in {c.conversation_id for c in repo.list_summaries()}


def test_delete_removes_conversation_and_unknown_ids_raise(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = repo.list_summaries()[0].conversation_id
    repo.delete(conversation_id=target)
    assert target not in {c.conversation_id for c in repo.list_summaries(include_archived=True)}
    with pytest.raises(ConversationNotFound):
        repo.unarchive(conversation_id=target)
    with pytest.raises(ConversationNotFound):
        repo.archive(conversation_id=uuid4())
