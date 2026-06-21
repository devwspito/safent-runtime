"""SqliteCapabilityBindingRepo — daemon-owned persistence for AgentCapabilityBinding (T019).

Follows the same WAL autocommit pattern as SqliteAgentRegistry and
SqlitePlatformModelRegistry. Single writer (daemon).

Schema is additive (expand-only). No PII stored — only domain ids and refs.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from hermes.capabilities.domain.agent_capability_binding import (
    AgentCapabilityBinding,
    BindingState,
)
from hermes.platforms.domain.ports import CapabilityBindingNotFound
from hermes.platforms.domain.value_objects import CapabilityRef

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_capability_bindings (
  binding_id          TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  agent_id            TEXT NOT NULL,
  capability_kind     TEXT NOT NULL,
  capability_id       TEXT NOT NULL,
  capability_version  TEXT NOT NULL,
  bound_by            INTEGER NOT NULL,
  state               TEXT NOT NULL DEFAULT 'bound',
  bound_at            TEXT NOT NULL,
  unbound_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_acb_agent ON agent_capability_bindings (agent_id, tenant_id);
CREATE INDEX IF NOT EXISTS idx_acb_cap ON agent_capability_bindings (capability_kind, capability_id);

CREATE TABLE IF NOT EXISTS agent_house_rule_overlays (
  overlay_id        TEXT PRIMARY KEY,
  agent_id          TEXT NOT NULL,
  platform_model_id TEXT NOT NULL,
  rule_id           TEXT NOT NULL,
  rule_kind         TEXT NOT NULL,
  target_area_ref   TEXT NOT NULL,
  phrasing          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ahro_agent ON agent_house_rule_overlays (agent_id);
"""


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class SqliteCapabilityBindingRepo:
    """WAL autocommit SQLite repository for AgentCapabilityBinding aggregates."""

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, binding: AgentCapabilityBinding) -> None:
        """Upsert a binding (INSERT OR REPLACE — idempotent by binding_id)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_capability_bindings
                  (binding_id, tenant_id, agent_id, capability_kind, capability_id,
                   capability_version, bound_by, state, bound_at, unbound_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.binding_id,
                    binding.tenant_id,
                    binding.agent_id,
                    binding.capability.kind,
                    binding.capability.capability_id,
                    binding.capability.version,
                    binding.bound_by,
                    str(binding.state),
                    binding.bound_at.isoformat(),
                    binding.unbound_at.isoformat() if binding.unbound_at else None,
                ),
            )

    def get(self, binding_id: str) -> AgentCapabilityBinding:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_capability_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
        if row is None:
            raise CapabilityBindingNotFound(binding_id)
        return self._row_to_binding(row)

    def list_by_agent(self, agent_id: str, tenant_id: str) -> list[AgentCapabilityBinding]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_capability_bindings
                WHERE agent_id=? AND tenant_id=? AND state='bound'
                ORDER BY bound_at DESC
                """,
                (agent_id, tenant_id),
            ).fetchall()
        return [self._row_to_binding(r) for r in rows]

    def find_active(
        self,
        agent_id: str,
        capability_kind: str,
        capability_id: str,
        tenant_id: str,
    ) -> AgentCapabilityBinding | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_capability_bindings
                WHERE agent_id=? AND capability_kind=? AND capability_id=?
                  AND tenant_id=? AND state='bound'
                ORDER BY bound_at DESC LIMIT 1
                """,
                (agent_id, capability_kind, capability_id, tenant_id),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_binding(row)

    def unbind(
        self,
        agent_id: str,
        capability_kind: str,
        capability_id: str,
        tenant_id: str,
    ) -> bool:
        """Mark matching active bindings as unbound. Returns True if any changed."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_capability_bindings
                SET state='unbound', unbound_at=?
                WHERE agent_id=? AND capability_kind=? AND capability_id=?
                  AND tenant_id=? AND state='bound'
                """,
                (_now_iso(), agent_id, capability_kind, capability_id, tenant_id),
            )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # AgentHouseRuleOverlay persistence
    # ------------------------------------------------------------------

    def save_overlay(self, overlay) -> None:
        """Upsert an AgentHouseRuleOverlay (INSERT OR REPLACE by overlay_id)."""
        from hermes.platforms.domain.agent_house_rule_overlay import AgentHouseRuleOverlay  # noqa: PLC0415

        if not isinstance(overlay, AgentHouseRuleOverlay):
            raise TypeError("Expected AgentHouseRuleOverlay")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_house_rule_overlays
                  (overlay_id, agent_id, platform_model_id, rule_id, rule_kind,
                   target_area_ref, phrasing)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    overlay.overlay_id,
                    overlay.agent_id,
                    overlay.platform_model_id,
                    overlay.house_rule.rule_id,
                    str(overlay.house_rule.kind),
                    overlay.house_rule.target_area_ref,
                    overlay.house_rule.phrasing,
                ),
            )

    def list_overlays_for_agent(self, agent_id: str, model_id: str) -> list:
        from hermes.platforms.domain.agent_house_rule_overlay import AgentHouseRuleOverlay  # noqa: PLC0415
        from hermes.platforms.domain.platform_model import HouseRule, HouseRuleKind  # noqa: PLC0415

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_house_rule_overlays
                WHERE agent_id=? AND platform_model_id=?
                """,
                (agent_id, model_id),
            ).fetchall()
        result = []
        for r in rows:
            rule = HouseRule(
                rule_id=r["rule_id"],
                kind=HouseRuleKind(r["rule_kind"]),
                target_area_ref=r["target_area_ref"],
                phrasing=r["phrasing"],
            )
            result.append(
                AgentHouseRuleOverlay(
                    overlay_id=r["overlay_id"],
                    agent_id=r["agent_id"],
                    platform_model_id=r["platform_model_id"],
                    house_rule=rule,
                )
            )
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_binding(row: sqlite3.Row) -> AgentCapabilityBinding:
        cap = CapabilityRef(
            kind=row["capability_kind"],
            capability_id=row["capability_id"],
            version=row["capability_version"],
        )
        return AgentCapabilityBinding(
            binding_id=row["binding_id"],
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            capability=cap,
            bound_by=row["bound_by"],
            state=BindingState(row["state"]),
            bound_at=datetime.fromisoformat(row["bound_at"]),
            unbound_at=datetime.fromisoformat(row["unbound_at"]) if row["unbound_at"] else None,
        )
