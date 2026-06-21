"""SqliteAgentRegistry — registro de agentes propiedad del daemon (shell-state.db).

Único escritor del estado de agentes. Siembra el agente 'default' en la primera
construcción (idempotente). WAL + autocommit, patrón de SQLiteConversationRepository.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from hermes.agents.domain.agent import (
    DEFAULT_AGENT_ID,
    Agent,
    AgentDraft,
    AutonomyLevel,
    default_agent,
)
from hermes.agents.domain.ports import (
    AgentNotFound,
    CannotDeleteDefaultAgent,
    CannotDeleteLastAgent,
)
from hermes.prompts.persona import PersonaSpec

_ACTIVE_KEY = "active_agent_id"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  agent_id          TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  color             TEXT NOT NULL,
  role              TEXT NOT NULL DEFAULT '',
  register_tone     TEXT NOT NULL DEFAULT '',
  primary_mission   TEXT NOT NULL DEFAULT '',
  instructions      TEXT NOT NULL DEFAULT '',
  language          TEXT NOT NULL DEFAULT 'es-ES',
  golden_rules      TEXT NOT NULL DEFAULT '[]',
  forbidden_phrases TEXT NOT NULL DEFAULT '[]',
  is_default        INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# Idempotent migration: adds autonomy_level to existing DBs without it.
# ALTER TABLE ADD COLUMN is a no-op if the column already exists in SQLite ≥ 3.37;
# older versions raise OperationalError — we suppress it explicitly.
_MIGRATION_AUTONOMY_LEVEL = (
    "ALTER TABLE agents ADD COLUMN autonomy_level TEXT NOT NULL DEFAULT 'balanced'"
)


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class SqliteAgentRegistry:
    """Registro de agentes en SQLite WAL, propiedad del daemon."""

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._migrate_autonomy_level(conn)
        self._ensure_default()

    @staticmethod
    def _migrate_autonomy_level(conn: sqlite3.Connection) -> None:
        """Añade autonomy_level a DBs existentes sin la columna (idempotente)."""
        try:
            conn.execute(_MIGRATION_AUTONOMY_LEVEL)
        except sqlite3.OperationalError:
            # La columna ya existe — migration idempotente.
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Seed
    # ------------------------------------------------------------------
    def _ensure_default(self) -> None:
        """Siembra el agente 'default' + lo marca activo si no hay agentes."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()
            if row["n"] > 0:
                return
            self._insert(conn, default_agent())
            conn.execute(
                "INSERT OR REPLACE INTO agent_settings (key, value) VALUES (?, ?)",
                (_ACTIVE_KEY, DEFAULT_AGENT_ID),
            )

    # ------------------------------------------------------------------
    # Mappers
    # ------------------------------------------------------------------
    @staticmethod
    def _insert(conn: sqlite3.Connection, agent: Agent) -> None:
        # OR IGNORE: el seed del 'default' (PK fija) es race-safe si el daemon y
        # el shell-server construyen el registro a la vez sobre la misma DB.
        conn.execute(
            """
            INSERT OR IGNORE INTO agents (
              agent_id, name, color, role, register_tone, primary_mission,
              instructions, language, golden_rules, forbidden_phrases,
              is_default, autonomy_level, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent.agent_id,
                agent.name,
                agent.color,
                agent.role,
                agent.register,
                agent.primary_mission,
                agent.instructions,
                agent.language,
                json.dumps(list(agent.golden_rules), ensure_ascii=False),
                json.dumps(list(agent.forbidden_phrases), ensure_ascii=False),
                1 if agent.is_default else 0,
                agent.autonomy_level.value,
                agent.created_at.isoformat(),
                agent.updated_at.isoformat(),
            ),
        )

    @staticmethod
    def _row_to_agent(row: sqlite3.Row) -> Agent:
        raw_autonomy = row["autonomy_level"] if "autonomy_level" in row.keys() else "balanced"
        try:
            autonomy = AutonomyLevel(raw_autonomy)
        except ValueError:
            # Dato corrupto en DB → default conservador (fail-safe).
            autonomy = AutonomyLevel.BALANCED
        return Agent(
            agent_id=row["agent_id"],
            name=row["name"],
            color=row["color"],
            role=row["role"],
            register=row["register_tone"],
            primary_mission=row["primary_mission"],
            instructions=row["instructions"],
            language=row["language"],
            golden_rules=tuple(json.loads(row["golden_rules"])),
            forbidden_phrases=tuple(json.loads(row["forbidden_phrases"])),
            is_default=bool(row["is_default"]),
            autonomy_level=autonomy,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def list_agents(self) -> list[Agent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agents ORDER BY is_default DESC, created_at ASC"
            ).fetchall()
        return [self._row_to_agent(r) for r in rows]

    def get_agent(self, agent_id: str) -> Agent:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        if row is None:
            raise AgentNotFound(agent_id)
        return self._row_to_agent(row)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def create_agent(self, draft: AgentDraft) -> Agent:
        now = datetime.now(tz=UTC)
        agent = Agent(
            agent_id=uuid4().hex,
            name=draft.name,
            role=draft.role,
            register=draft.register,
            primary_mission=draft.primary_mission,
            instructions=draft.instructions,
            color=draft.color,
            language=draft.language,
            golden_rules=draft.golden_rules,
            forbidden_phrases=draft.forbidden_phrases,
            is_default=False,
            autonomy_level=draft.autonomy_level,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            self._insert(conn, agent)
        return agent

    def update_agent(self, agent_id: str, draft: AgentDraft) -> Agent:
        existing = self.get_agent(agent_id)  # raises AgentNotFound
        if existing.is_default:
            # El Cerebro (default) tiene un system prompt FIJO world-class: su
            # role/misión/golden_rules/nombre/forbidden NO son editables (un prompt
            # malo haría parecer roto el SO). Solo se aceptan el TONO (register), la
            # PERSONALIDAD extra (instructions, que se SUMA al prompt) y el color.
            # La autonomía del Cerebro queda fija (AUTONOMOUS, omnipotente).
            base = default_agent()
            updated = Agent(
                agent_id=existing.agent_id,
                name=base.name,
                role=base.role,
                register=draft.register.strip() or base.register,
                primary_mission=base.primary_mission,
                instructions=draft.instructions,
                color=draft.color or base.color,
                language=base.language,
                golden_rules=base.golden_rules,
                forbidden_phrases=base.forbidden_phrases,
                is_default=True,
                autonomy_level=base.autonomy_level,
                created_at=existing.created_at,
                updated_at=datetime.now(tz=UTC),
            )
        else:
            updated = Agent(
                agent_id=existing.agent_id,
                name=draft.name,
                role=draft.role,
                register=draft.register,
                primary_mission=draft.primary_mission,
                instructions=draft.instructions,
                color=draft.color,
                language=draft.language,
                golden_rules=draft.golden_rules,
                forbidden_phrases=draft.forbidden_phrases,
                is_default=existing.is_default,
                autonomy_level=draft.autonomy_level,
                created_at=existing.created_at,
                updated_at=datetime.now(tz=UTC),
            )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agents SET
                  name = ?, color = ?, role = ?, register_tone = ?,
                  primary_mission = ?, instructions = ?, language = ?,
                  golden_rules = ?, forbidden_phrases = ?,
                  autonomy_level = ?, updated_at = ?
                WHERE agent_id = ?
                """,
                (
                    updated.name,
                    updated.color,
                    updated.role,
                    updated.register,
                    updated.primary_mission,
                    updated.instructions,
                    updated.language,
                    json.dumps(list(updated.golden_rules), ensure_ascii=False),
                    json.dumps(list(updated.forbidden_phrases), ensure_ascii=False),
                    updated.autonomy_level.value,
                    updated.updated_at.isoformat(),
                    agent_id,
                ),
            )
        return updated

    def delete_agent(self, agent_id: str) -> None:
        agent = self.get_agent(agent_id)  # raises AgentNotFound
        if agent.is_default:
            raise CannotDeleteDefaultAgent(agent_id)
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"]
            if count <= 1:
                raise CannotDeleteLastAgent(agent_id)
            conn.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
            # Si era el activo, reactiva el default.
            if self._read_setting(conn, _ACTIVE_KEY) == agent_id:
                conn.execute(
                    "INSERT OR REPLACE INTO agent_settings (key, value) VALUES (?, ?)",
                    (_ACTIVE_KEY, DEFAULT_AGENT_ID),
                )

    # ------------------------------------------------------------------
    # Active agent
    # ------------------------------------------------------------------
    @staticmethod
    def _read_setting(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM agent_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def active_agent_id(self) -> str:
        with self._connect() as conn:
            value = self._read_setting(conn, _ACTIVE_KEY)
            if value is not None:
                exists = conn.execute(
                    "SELECT 1 FROM agents WHERE agent_id = ?", (value,)
                ).fetchone()
                if exists:
                    return value
            # Fallback: default si existe, si no el primero.
            row = conn.execute(
                "SELECT agent_id FROM agents ORDER BY is_default DESC, created_at ASC LIMIT 1"
            ).fetchone()
        return row["agent_id"] if row else DEFAULT_AGENT_ID

    def set_active_agent(self, agent_id: str) -> None:
        self.get_agent(agent_id)  # raises AgentNotFound
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_settings (key, value) VALUES (?, ?)",
                (_ACTIVE_KEY, agent_id),
            )

    # ------------------------------------------------------------------
    # Persona resolution (consumido por el engine, por ciclo)
    # ------------------------------------------------------------------
    def persona_for(self, agent_id: str | None) -> PersonaSpec:
        target = agent_id or self.active_agent_id()
        try:
            return self.get_agent(target).to_persona()
        except AgentNotFound:
            pass
        # Fail-soft: el agente activo, o el default hardcoded.
        try:
            return self.get_agent(self.active_agent_id()).to_persona()
        except AgentNotFound:
            return default_agent().to_persona()
