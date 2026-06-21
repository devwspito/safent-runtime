"""SQLite repository for remote_control_sessions.

Schema lives in ops/agents-os-edition/migrations/sqlite/001_initial_personal_desktop.sql.
Mirror the columns we actually need at shell-server layer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional
from uuid import UUID


_REMOTE_CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS remote_control_sessions (
  remote_control_session_id    TEXT PRIMARY KEY,
  node_installation_id         TEXT NOT NULL,
  tenant_id                    TEXT NOT NULL,
  operator_id                  TEXT NOT NULL,
  scope                        TEXT NOT NULL,
  token_ciphertext             BLOB NOT NULL,
  token_kid                    TEXT NOT NULL,
  token_alg                    TEXT NOT NULL DEFAULT 'AES-GCM-256',
  token_expires_at             TEXT NOT NULL,
  dtls_fingerprint             TEXT NOT NULL,
  binding_hash                 TEXT NOT NULL,
  consent_id                   TEXT,
  state                        TEXT NOT NULL DEFAULT 'issued',
  issued_at                    TEXT NOT NULL,
  accepted_at                  TEXT,
  ended_at                     TEXT,
  end_reason                   TEXT,
  captured_training_steps_count INTEGER NOT NULL DEFAULT 0,
  redeemed_at                  TEXT,
  redeem_ip                    TEXT,
  redeem_user_agent            TEXT
);
CREATE INDEX IF NOT EXISTS idx_rcs_tenant_state
  ON remote_control_sessions (tenant_id, state);
CREATE INDEX IF NOT EXISTS idx_rcs_expires
  ON remote_control_sessions (token_expires_at);
"""


@dataclass(frozen=True, slots=True)
class RemoteControlRow:
    session_id: UUID
    node_installation_id: UUID
    tenant_id: UUID
    operator_id: UUID
    scope: str
    token_ciphertext: bytes
    token_kid: str
    token_expires_at: datetime
    dtls_fingerprint: str
    binding_hash: str
    consent_id: Optional[UUID]
    state: str
    issued_at: datetime
    accepted_at: Optional[datetime]
    ended_at: Optional[datetime]
    end_reason: Optional[str]
    redeemed_at: Optional[datetime]
    redeem_ip: Optional[str]
    redeem_user_agent: Optional[str]


def _conn(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _row_to_dto(row: sqlite3.Row) -> RemoteControlRow:
    return RemoteControlRow(
        session_id=UUID(row["remote_control_session_id"]),
        node_installation_id=UUID(row["node_installation_id"]),
        tenant_id=UUID(row["tenant_id"]),
        operator_id=UUID(row["operator_id"]),
        scope=row["scope"],
        token_ciphertext=row["token_ciphertext"],
        token_kid=row["token_kid"],
        token_expires_at=_parse_dt(row["token_expires_at"]),  # type: ignore[arg-type]
        dtls_fingerprint=row["dtls_fingerprint"],
        binding_hash=row["binding_hash"],
        consent_id=UUID(row["consent_id"]) if row["consent_id"] else None,
        state=row["state"],
        issued_at=_parse_dt(row["issued_at"]),  # type: ignore[arg-type]
        accepted_at=_parse_dt(row["accepted_at"]),
        ended_at=_parse_dt(row["ended_at"]),
        end_reason=row["end_reason"],
        redeemed_at=_parse_dt(row["redeemed_at"]),
        redeem_ip=row["redeem_ip"],
        redeem_user_agent=row["redeem_user_agent"],
    )


class SQLiteRemoteControlRepo:
    def __init__(self, db_path: Path) -> None:
        self._db = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with _conn(db_path) as c:
            c.executescript("PRAGMA journal_mode=WAL;")
            c.executescript(_REMOTE_CONTROL_SCHEMA)

    def insert(self, row: RemoteControlRow) -> None:
        with _conn(self._db) as c:
            c.execute(
                """
                INSERT INTO remote_control_sessions (
                  remote_control_session_id, node_installation_id, tenant_id,
                  operator_id, scope, token_ciphertext, token_kid,
                  token_expires_at, dtls_fingerprint, binding_hash, consent_id,
                  state, issued_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(row.session_id),
                    str(row.node_installation_id),
                    str(row.tenant_id),
                    str(row.operator_id),
                    row.scope,
                    row.token_ciphertext,
                    row.token_kid,
                    row.token_expires_at.isoformat(),
                    row.dtls_fingerprint,
                    row.binding_hash,
                    str(row.consent_id) if row.consent_id else None,
                    row.state,
                    row.issued_at.isoformat(),
                ),
            )

    def get(self, session_id: UUID) -> Optional[RemoteControlRow]:
        with _conn(self._db) as c:
            row = c.execute(
                "SELECT * FROM remote_control_sessions "
                "WHERE remote_control_session_id = ?",
                (str(session_id),),
            ).fetchone()
        return _row_to_dto(row) if row else None

    def list_active(self, limit: int = 50) -> list[RemoteControlRow]:
        with _conn(self._db) as c:
            rows = c.execute(
                "SELECT * FROM remote_control_sessions "
                "WHERE state IN ('issued','accepted','active') "
                "ORDER BY issued_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dto(r) for r in rows]

    def mark_redeemed(
        self,
        session_id: UUID,
        *,
        ip: str,
        user_agent: str,
    ) -> int:
        now = datetime.now(tz=UTC).isoformat()
        with _conn(self._db) as c:
            res = c.execute(
                """
                UPDATE remote_control_sessions
                   SET redeemed_at = ?, redeem_ip = ?, redeem_user_agent = ?,
                       state = 'accepted', accepted_at = ?
                 WHERE remote_control_session_id = ?
                   AND state = 'issued'
                   AND redeemed_at IS NULL
                """,
                (now, ip, user_agent, now, str(session_id)),
            )
            return res.rowcount

    def transition_state(
        self,
        session_id: UUID,
        new_state: str,
        *,
        from_states: tuple[str, ...],
        end_reason: Optional[str] = None,
    ) -> int:
        now = datetime.now(tz=UTC).isoformat()
        placeholders = ",".join("?" for _ in from_states)
        params = [new_state]
        sets = ["state = ?"]
        if new_state == "ended":
            sets.append("ended_at = ?")
            params.append(now)
            if end_reason:
                sets.append("end_reason = ?")
                params.append(end_reason)
        elif new_state == "active":
            sets.append("accepted_at = COALESCE(accepted_at, ?)")
            params.append(now)
        sql = (
            f"UPDATE remote_control_sessions SET {', '.join(sets)} "
            f"WHERE remote_control_session_id = ? AND state IN ({placeholders})"
        )
        params.append(str(session_id))
        params.extend(from_states)
        with _conn(self._db) as c:
            res = c.execute(sql, tuple(params))
            return res.rowcount
