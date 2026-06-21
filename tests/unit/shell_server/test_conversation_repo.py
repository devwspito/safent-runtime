"""Tests SQLiteConversationRepository."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from hermes.shell_server.chat.conversation_repo import (
    ConversationNotFound,
    SQLiteConversationRepository,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path: Path) -> SQLiteConversationRepository:
    return SQLiteConversationRepository(db_path=tmp_path / "conv.db")


class TestCreateAndAppend:
    def test_create_and_list(self, repo: SQLiteConversationRepository) -> None:
        cid = uuid4()
        repo.create_or_touch(
            conversation_id=cid,
            first_user_message="Hola mundo",
            provider_alias="vLLM",
            model="qwen3",
        )
        items = repo.list_summaries()
        assert len(items) == 1
        assert items[0].conversation_id == cid
        assert items[0].title == "Hola mundo"
        assert items[0].provider_alias == "vLLM"
        assert items[0].message_count == 0

    def test_title_truncation(self, repo: SQLiteConversationRepository) -> None:
        cid = uuid4()
        long = "a" * 200
        repo.create_or_touch(
            conversation_id=cid, first_user_message=long
        )
        d = repo.get_detail(conversation_id=cid)
        assert len(d.title) <= 60
        assert d.title.endswith("…")

    def test_append_messages(self, repo: SQLiteConversationRepository) -> None:
        cid = uuid4()
        repo.create_or_touch(
            conversation_id=cid, first_user_message="Q1"
        )
        repo.append_message(conversation_id=cid, role="user", content="Q1")
        repo.append_message(conversation_id=cid, role="assistant", content="R1")
        d = repo.get_detail(conversation_id=cid)
        assert len(d.messages) == 2
        assert d.messages[0].role == "user"
        assert d.messages[1].content == "R1"


class TestDelete:
    def test_delete(self, repo: SQLiteConversationRepository) -> None:
        cid = uuid4()
        repo.create_or_touch(conversation_id=cid, first_user_message="x")
        repo.delete(conversation_id=cid)
        assert repo.list_summaries() == []

    def test_delete_unknown(self, repo: SQLiteConversationRepository) -> None:
        with pytest.raises(ConversationNotFound):
            repo.delete(conversation_id=uuid4())


class TestArchive:
    def test_archive_hides(self, repo: SQLiteConversationRepository) -> None:
        cid = uuid4()
        repo.create_or_touch(conversation_id=cid, first_user_message="x")
        repo.archive(conversation_id=cid)
        assert repo.list_summaries() == []
        assert len(repo.list_summaries(include_archived=True)) == 1
