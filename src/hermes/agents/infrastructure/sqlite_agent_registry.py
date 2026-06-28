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
    CannotUpdateDefaultAgent,
)
from hermes.agents.domain.default_roster import default_roster
from hermes.prompts.persona import PersonaSpec

_ROSTER_SEEDED_KEY = "roster_seeded"
# Toggle del equipo por defecto: cuando está OFF, los 27 especialistas sembrados
# (id con prefijo `roster-`) se OCULTAN de list_agents (vista Agentes, delegación) —
# NO se borran, así re-activar es instantáneo y conserva ediciones. El CEO (id `default`)
# y los agentes propios del usuario (uuid4, sin prefijo) SIEMPRE quedan.
_DEFAULT_ROSTER_ENABLED_KEY = "default_roster_enabled"
_ROSTER_ID_PREFIX = "roster-"

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

# Idempotent migration: adds department to existing DBs without it.
# NULL default → treated as "mis-agentes" by the roster endpoint.
_MIGRATION_DEPARTMENT = "ALTER TABLE agents ADD COLUMN department TEXT"

# Idempotent migration: adds provider_alias to existing DBs without it.
# NULL default → agent uses the globally active provider (fallback).
_MIGRATION_PROVIDER_ALIAS = "ALTER TABLE agents ADD COLUMN provider_alias TEXT"

# Idempotent migration: adds managed_by to existing DBs without it.
# NULL = local (owner-created). "cloud" = pushed by the config-sync applier.
# The applier uses this to reconcile: cloud-managed agents absent from the
# bundle are deleted; locally-created agents are never touched.
_MIGRATION_MANAGED_BY = "ALTER TABLE agents ADD COLUMN managed_by TEXT"


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
            self._migrate_department(conn)
            self._migrate_provider_alias(conn)
            self._migrate_managed_by(conn)
        self._ensure_default()
        self._seed_roster()

    @staticmethod
    def _migrate_autonomy_level(conn: sqlite3.Connection) -> None:
        """Añade autonomy_level a DBs existentes sin la columna (idempotente)."""
        try:
            conn.execute(_MIGRATION_AUTONOMY_LEVEL)
        except sqlite3.OperationalError:
            # La columna ya existe — migration idempotente.
            pass

    @staticmethod
    def _migrate_department(conn: sqlite3.Connection) -> None:
        """Añade department (nullable) a DBs existentes sin la columna (idempotente)."""
        try:
            conn.execute(_MIGRATION_DEPARTMENT)
        except sqlite3.OperationalError:
            # La columna ya existe — migration idempotente.
            pass

    @staticmethod
    def _migrate_provider_alias(conn: sqlite3.Connection) -> None:
        """Añade provider_alias (nullable) a DBs existentes sin la columna (idempotente)."""
        try:
            conn.execute(_MIGRATION_PROVIDER_ALIAS)
        except sqlite3.OperationalError:
            # La columna ya existe — migration idempotente.
            pass

    @staticmethod
    def _migrate_managed_by(conn: sqlite3.Connection) -> None:
        """Añade managed_by (nullable) a DBs existentes sin la columna (idempotente).

        NULL = local (owner-created). 'cloud' = pushed by config-sync applier.
        """
        try:
            conn.execute(_MIGRATION_MANAGED_BY)
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
        """Siembra el agente 'default' (CEO) si no hay agentes."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()
            if row["n"] > 0:
                return
            self._insert(conn, default_agent())

    def _seed_roster(self) -> None:
        """Siembra el equipo de fábrica UNA sola vez (flag en agent_settings).

        Gated por flag (no por COUNT): si el dueño borra un especialista, NO reaparece
        en el siguiente arranque. INSERT OR IGNORE por PK fija = race-safe entre daemon
        y shell-server.
        """
        with self._connect() as conn:
            seeded = conn.execute(
                "SELECT value FROM agent_settings WHERE key = ?", (_ROSTER_SEEDED_KEY,)
            ).fetchone()
            if seeded is not None:
                return
            for agent in default_roster():
                self._insert(conn, agent)
            conn.execute(
                "INSERT OR REPLACE INTO agent_settings (key, value) VALUES (?, ?)",
                (_ROSTER_SEEDED_KEY, _now_iso()),
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
              is_default, autonomy_level, department, provider_alias,
              managed_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                agent.department,
                agent.provider_alias,
                agent.managed_by,
                agent.created_at.isoformat(),
                agent.updated_at.isoformat(),
            ),
        )

    @staticmethod
    def _row_to_agent(row: sqlite3.Row) -> Agent:
        keys = row.keys()
        raw_autonomy = row["autonomy_level"] if "autonomy_level" in keys else "balanced"
        try:
            autonomy = AutonomyLevel(raw_autonomy)
        except ValueError:
            # Dato corrupto en DB → default conservador (fail-safe).
            autonomy = AutonomyLevel.BALANCED
        department: str | None = row["department"] if "department" in keys else None
        provider_alias: str | None = row["provider_alias"] if "provider_alias" in keys else None
        managed_by: str | None = row["managed_by"] if "managed_by" in keys else None
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
            department=department,
            provider_alias=provider_alias,
            managed_by=managed_by,
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
            roster_on = self._read_setting(conn, _DEFAULT_ROSTER_ENABLED_KEY) != "0"
        agents = [self._row_to_agent(r) for r in rows]
        if not roster_on:
            # Equipo por defecto APAGADO: ocultar los 27 especialistas sembrados
            # (id `roster-*`). El CEO (`default`) y los agentes propios siguen visibles.
            agents = [a for a in agents if not a.agent_id.startswith(_ROSTER_ID_PREFIX)]
        return agents

    def default_roster_enabled(self) -> bool:
        """¿Está visible el equipo de especialistas por defecto? (ON por defecto)."""
        with self._connect() as conn:
            return self._read_setting(conn, _DEFAULT_ROSTER_ENABLED_KEY) != "0"

    def set_default_roster_enabled(self, enabled: bool) -> None:
        """Enciende/apaga el equipo por defecto (filtra, NO borra — reversible)."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_settings (key, value) VALUES (?, ?)",
                (_DEFAULT_ROSTER_ENABLED_KEY, "1" if enabled else "0"),
            )

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
            # Honor a caller-provided id (cloud config-sync passes the stable
            # agent_template_id so re-syncs upsert instead of duplicating); fall
            # back to a fresh uuid for native UI creates.
            agent_id=draft.agent_id or uuid4().hex,
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
            department=draft.department,
            provider_alias=draft.provider_alias,
            managed_by=draft.managed_by,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            self._insert(conn, agent)
        return agent

    def update_agent(self, agent_id: str, draft: AgentDraft) -> Agent:
        existing = self.get_agent(agent_id)  # raises AgentNotFound
        if existing.is_default:
            raise CannotUpdateDefaultAgent(agent_id)
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
                department=draft.department,
                provider_alias=draft.provider_alias,
                # Preserve managed_by from the draft; allows re-stamping cloud ownership on update.
                managed_by=draft.managed_by if draft.managed_by is not None else existing.managed_by,
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
                  autonomy_level = ?, department = ?, provider_alias = ?,
                  managed_by = ?, updated_at = ?
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
                    updated.department,
                    updated.provider_alias,
                    updated.managed_by,
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

    # ------------------------------------------------------------------
    # Settings helpers (used by roster seeding)
    # ------------------------------------------------------------------
    @staticmethod
    def _read_setting(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM agent_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    # ------------------------------------------------------------------
    # Persona resolution (consumido por el engine, por ciclo)
    # ------------------------------------------------------------------
    def persona_for(self, agent_id: str | None) -> PersonaSpec:
        target = agent_id or DEFAULT_AGENT_ID
        try:
            return self.get_agent(target).to_persona()
        except AgentNotFound:
            pass
        # Fail-soft: the CEO (default), or the hardcoded default_agent().
        try:
            return self.get_agent(DEFAULT_AGENT_ID).to_persona()
        except AgentNotFound:
            return default_agent().to_persona()
