"""ConversationRepository — persistencia de conversaciones.

Schema:

  conversations
    conversation_id   TEXT PK
    title             TEXT          -- truncado del primer user msg
    provider_alias    TEXT          -- snapshot del provider activo
    model             TEXT          -- snapshot del modelo
    started_at        TEXT (iso)
    last_msg_at       TEXT (iso)
    archived          INTEGER (0/1)

  messages
    message_id        TEXT PK
    conversation_id   TEXT FK
    role              TEXT          -- user|assistant|tool|system
    content           TEXT
    created_at        TEXT (iso)
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from dataclasses import dataclass as _dataclass


@_dataclass(frozen=True, slots=True)
class ChatMessage:
    """Mensaje del historial conversacional (user / assistant). Estructura
    mínima compartida por el persistor y los engines (Nous, hermes-agent)."""

    role: str
    content: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  conversation_id TEXT PRIMARY KEY,
  title           TEXT NOT NULL,
  provider_alias  TEXT,
  model           TEXT,
  started_at      TEXT NOT NULL,
  last_msg_at     TEXT NOT NULL,
  archived        INTEGER NOT NULL DEFAULT 0,
  agent_id        TEXT
);

CREATE INDEX IF NOT EXISTS conv_last_msg_idx
  ON conversations (last_msg_at DESC) WHERE archived = 0;

CREATE TABLE IF NOT EXISTS messages (
  message_id      TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  role            TEXT NOT NULL,
  content         TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS msg_conv_idx
  ON messages (conversation_id, created_at);
"""


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _title_from(text: str, *, max_chars: int = 60) -> str:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


@dataclass(slots=True)
class ConversationSummary:
    conversation_id: UUID
    title: str
    provider_alias: str | None
    model: str | None
    started_at: datetime
    last_msg_at: datetime
    archived: bool
    message_count: int = 0
    agent_id: str | None = None


@dataclass(slots=True)
class ConversationDetail:
    conversation_id: UUID
    title: str
    provider_alias: str | None
    model: str | None
    started_at: datetime
    last_msg_at: datetime
    archived: bool
    messages: list[ChatMessage] = field(default_factory=list)


class ConversationNotFound(LookupError):
    pass


class SQLiteConversationRepository:
    """Persistence de conversations en SQLite WAL."""

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            # Migración idempotente: agent_id en DBs creadas antes del multi-agente.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
            if "agent_id" not in cols:
                conn.execute("ALTER TABLE conversations ADD COLUMN agent_id TEXT")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------------------------------------------------------
    # Create / update
    # ---------------------------------------------------------------
    def create_or_touch(
        self,
        *,
        conversation_id: UUID,
        first_user_message: str,
        provider_alias: str | None = None,
        model: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Crea la conversation si no existe (toma title del primer mensaje).

        agent_id ata la conversación al agente del roster que la atiende, para
        que "Recientes" pueda filtrar por agente activo.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (str(conversation_id),),
            ).fetchone()
            if row is None:
                now = _now_iso()
                conn.execute(
                    """
                    INSERT INTO conversations (
                      conversation_id, title, provider_alias, model,
                      started_at, last_msg_at, archived, agent_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        str(conversation_id),
                        _title_from(first_user_message),
                        provider_alias,
                        model,
                        now,
                        now,
                        agent_id,
                    ),
                )

    def append_message(
        self,
        *,
        conversation_id: UUID,
        role: str,
        content: str,
    ) -> None:

        with self._connect() as conn:
            now = _now_iso()
            conn.execute(
                """
                INSERT INTO messages (
                  message_id, conversation_id, role, content, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid4()), str(conversation_id), role, content, now),
            )
            conn.execute(
                "UPDATE conversations SET last_msg_at = ? "
                "WHERE conversation_id = ?",
                (now, str(conversation_id)),
            )

    def archive(self, *, conversation_id: UUID) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE conversations SET archived = 1 "
                "WHERE conversation_id = ?",
                (str(conversation_id),),
            )
            if cursor.rowcount == 0:
                raise ConversationNotFound(str(conversation_id))

    def delete(self, *, conversation_id: UUID) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                (str(conversation_id),),
            )
            if cursor.rowcount == 0:
                raise ConversationNotFound(str(conversation_id))

    # ---------------------------------------------------------------
    # Queries
    # ---------------------------------------------------------------
    def list_summaries(
        self,
        *,
        include_archived: bool = False,
        limit: int = 100,
        agent_id: str | None = None,
    ) -> list[ConversationSummary]:
        conditions: list[str] = []
        params: list[object] = []
        if not include_archived:
            conditions.append("c.archived = 0")
        if agent_id is not None:
            conditions.append("c.agent_id = ?")
            params.append(agent_id)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"""
            SELECT c.*, COUNT(m.message_id) AS msg_count
              FROM conversations c
              LEFT JOIN messages m ON m.conversation_id = c.conversation_id
              {where}
              GROUP BY c.conversation_id
              ORDER BY c.last_msg_at DESC
              LIMIT ?
        """
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            ConversationSummary(
                conversation_id=UUID(r["conversation_id"]),
                title=r["title"],
                provider_alias=r["provider_alias"],
                model=r["model"],
                started_at=datetime.fromisoformat(r["started_at"]),
                last_msg_at=datetime.fromisoformat(r["last_msg_at"]),
                archived=bool(r["archived"]),
                message_count=int(r["msg_count"] or 0),
                agent_id=r["agent_id"],
            )
            for r in rows
        ]

    def get_detail(self, *, conversation_id: UUID) -> ConversationDetail:
        with self._connect() as conn:
            crow = conn.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (str(conversation_id),),
            ).fetchone()
            if crow is None:
                raise ConversationNotFound(str(conversation_id))
            mrows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? "
                "ORDER BY created_at",
                (str(conversation_id),),
            ).fetchall()
        return ConversationDetail(
            conversation_id=UUID(crow["conversation_id"]),
            title=crow["title"],
            provider_alias=crow["provider_alias"],
            model=crow["model"],
            started_at=datetime.fromisoformat(crow["started_at"]),
            last_msg_at=datetime.fromisoformat(crow["last_msg_at"]),
            archived=bool(crow["archived"]),
            messages=[
                ChatMessage(role=r["role"], content=r["content"]) for r in mrows
            ],
        )

    def load_messages(
        self, *, conversation_id: UUID
    ) -> list[ChatMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages "
                "WHERE conversation_id = ? ORDER BY created_at",
                (str(conversation_id),),
            ).fetchall()
        return [ChatMessage(role=r["role"], content=r["content"]) for r in rows]
