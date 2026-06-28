"""Conversaciones tagueadas por agente (Recientes por agente activo)."""

from __future__ import annotations

from uuid import uuid4

from hermes.tasks.infrastructure.sqlite_conversation_repo import SQLiteConversationRepository


def test_tag_and_filter_by_agent(tmp_path):
    repo = SQLiteConversationRepository(db_path=tmp_path / "shell-state.db")
    a, b = uuid4(), uuid4()
    repo.create_or_touch(conversation_id=a, first_user_message="hola A", agent_id="default")
    repo.create_or_touch(conversation_id=b, first_user_message="hola B", agent_id="ventas")

    assert len(repo.list_summaries()) == 2  # sin filtro: todas
    ventas = repo.list_summaries(agent_id="ventas")
    assert len(ventas) == 1
    assert ventas[0].agent_id == "ventas"
    assert ventas[0].title == "hola B"


def test_idempotent_migration_adds_agent_id(tmp_path):
    # Simula una DB pre-multiagente sin la columna agent_id.
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE conversations (
          conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL,
          provider_alias TEXT, model TEXT, started_at TEXT NOT NULL,
          last_msg_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE messages (
          message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
          role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    # Al abrir con el repo, la migración idempotente añade agent_id sin romper.
    repo = SQLiteConversationRepository(db_path=db)
    c = uuid4()
    repo.create_or_touch(conversation_id=c, first_user_message="x", agent_id="default")
    assert repo.list_summaries(agent_id="default")[0].agent_id == "default"
