"""Unit — migración del CHECK de `status` de `pending_approvals` → admite 'expired'.

Regresión del loop de tarjetas fantasma (2026-07). `SqliteApprovalGate.expire()`
escribe status='expired' cuando la espera del dueño caduca, para sacar la fila de
la lista de pendientes (list_hitl_pending filtra status='pending'). El CHECK
original SOLO admitía ('pending','approved','rejected'), así que expire() lanzaba
sqlite3.IntegrityError — tragado por los callers (except: pass) — y la fila se
quedaba 'pending' PARA SIEMPRE, re-apareciendo como tarjeta fantasma.

Cubre:
  (a) gate.expire() sobre una fila 'pending' → status='expired', excluida de la
      lista de pendientes, sin IntegrityError (fresh DB y DB legacy migrada).
  (b) la recreación de tabla es idempotente: re-correr ensure_capabilities_schema
      no recrea ni pierde filas (counts preservados, datos intactos).
  (c) los estados existentes ('pending'/'approved'/'rejected') siguen aceptados;
      un estado inválido lo sigue rechazando el nuevo CHECK.

Unit puro sobre SQLite local — sin binarios externos ni red.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
from hermes.capabilities.domain.ports import ConsentContext, RiskLevel
from hermes.capabilities.infrastructure.schema import ensure_capabilities_schema
from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate

_NOW = "2026-07-04T12:00:00.000Z"

# DB "legacy": el CHECK viejo (sin 'expired') y las 4 columnas tardías
# (tool_name/action_digest/conversation_id/attempt_count) AL FINAL vía ALTER —
# orden físico distinto al de la tabla canónica nueva. Estresa el INSERT SELECT
# por-nombre de la migración (no debe depender de posiciones).
_LEGACY_DDL = """
CREATE TABLE pending_approvals (
    proposal_id          TEXT PRIMARY KEY,
    work_item_id         TEXT NOT NULL,
    tenant_id            TEXT,
    operator_id          TEXT NOT NULL,
    risk                 TEXT NOT NULL CHECK (risk IN ('low','high')),
    justification        TEXT,
    parameters_redacted  TEXT NOT NULL DEFAULT '{}',
    status               TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected')),
    approved_by          TEXT,
    token_hmac           TEXT,
    nonce                TEXT,
    expires_at           TEXT,
    consumed_at          TEXT,
    created_at           TEXT NOT NULL,
    resolved_at          TEXT
);
CREATE INDEX pending_approvals_work_item_idx ON pending_approvals (work_item_id);
"""

_LEGACY_ALTERS = (
    "ALTER TABLE pending_approvals ADD COLUMN tool_name TEXT",
    "ALTER TABLE pending_approvals ADD COLUMN action_digest TEXT",
    "ALTER TABLE pending_approvals ADD COLUMN conversation_id TEXT",
    "ALTER TABLE pending_approvals ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1",
)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _build_legacy_db(path: Path) -> None:
    """Crea `pending_approvals` con el CHECK VIEJO (sin 'expired') + columnas ALTER."""
    conn = _connect(path)
    conn.executescript(_LEGACY_DDL)
    for stmt in _LEGACY_ALTERS:
        conn.execute(stmt)
    conn.close()


def _insert_row(conn: sqlite3.Connection, **overrides) -> str:
    proposal_id = overrides.pop("proposal_id", str(uuid4()))
    cols = {
        "proposal_id": proposal_id,
        "work_item_id": str(uuid4()),
        "operator_id": str(uuid4()),
        "risk": "high",
        "status": "pending",
        "created_at": _NOW,
    }
    cols.update(overrides)
    placeholders = ", ".join(f":{k}" for k in cols)
    conn.execute(
        f"INSERT INTO pending_approvals ({', '.join(cols)}) VALUES ({placeholders})",
        cols,
    )
    return proposal_id


def _table_sql(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pending_approvals'"
    ).fetchone()
    return row[0] if row and row[0] else ""


def _pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM pending_approvals WHERE status='pending'"
    ).fetchone()["n"]


# ── (c) CHECK ampliado: 'expired' aceptado, existentes intactos, inválido out ──


def test_fresh_db_status_check_admits_expired(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    conn = _connect(db)
    ensure_capabilities_schema(conn)
    # El DDL de la tabla ya trae 'expired' en el CHECK (nace correcta → migración no-op).
    assert "'expired'" in _table_sql(conn)
    pid = _insert_row(conn)
    conn.execute(
        "UPDATE pending_approvals SET status='expired', resolved_at=? WHERE proposal_id=?",
        (_NOW, pid),
    )
    got = conn.execute(
        "SELECT status FROM pending_approvals WHERE proposal_id=?", (pid,)
    ).fetchone()["status"]
    conn.close()
    assert got == "expired"


def test_existing_statuses_still_accepted(tmp_path: Path) -> None:
    db = tmp_path / "states.db"
    conn = _connect(db)
    ensure_capabilities_schema(conn)
    assert _insert_row(conn, status="pending")
    assert _insert_row(conn, status="approved")
    assert _insert_row(conn, status="rejected")
    assert _insert_row(conn, status="expired")
    conn.close()


def test_invalid_status_still_rejected(tmp_path: Path) -> None:
    db = tmp_path / "invalid.db"
    conn = _connect(db)
    ensure_capabilities_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_row(conn, status="consumed")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_row(conn, status="bogus")
    conn.close()


# ── (b) recreación de tabla legacy: preserva filas + idempotente ───────────────


def test_legacy_db_migrated_preserves_rows_and_admits_expired(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _build_legacy_db(db)

    # Filas con el esquema viejo, incluyendo valores en las columnas tardías.
    conn = _connect(db)
    assert "'expired'" not in _table_sql(conn)  # legacy: CHECK sin 'expired'
    pid_pending = _insert_row(
        conn, status="pending", tool_name="fs_write", action_digest="dig-1",
        conversation_id="conv-1",
    )
    pid_approved = _insert_row(conn, status="approved", tool_name="net_call")
    pid_rejected = _insert_row(conn, status="rejected")
    conn.close()

    # Migración vía ensure_capabilities_schema.
    conn = _connect(db)
    ensure_capabilities_schema(conn)

    # El CHECK ya admite 'expired'.
    assert "'expired'" in _table_sql(conn)

    # NINGUNA fila se perdió + valores de columnas tardías intactos.
    rows = {
        r["proposal_id"]: r
        for r in conn.execute(
            "SELECT proposal_id, status, tool_name, action_digest, "
            "conversation_id, attempt_count FROM pending_approvals"
        ).fetchall()
    }
    assert set(rows) == {pid_pending, pid_approved, pid_rejected}
    assert rows[pid_pending]["tool_name"] == "fs_write"
    assert rows[pid_pending]["action_digest"] == "dig-1"
    assert rows[pid_pending]["conversation_id"] == "conv-1"
    assert rows[pid_pending]["attempt_count"] == 1
    assert rows[pid_approved]["status"] == "approved"

    # 'expired' ahora aceptado sobre una fila real.
    conn.execute(
        "UPDATE pending_approvals SET status='expired', resolved_at=? WHERE proposal_id=?",
        (_NOW, pid_pending),
    )
    assert (
        conn.execute(
            "SELECT status FROM pending_approvals WHERE proposal_id=?", (pid_pending,)
        ).fetchone()["status"]
        == "expired"
    )

    # Los índices se recrearon tras el DROP TABLE de la migración.
    indexes = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='pending_approvals'"
        ).fetchall()
    }
    assert {
        "pending_approvals_work_item_idx",
        "pending_approvals_action_digest_idx",
        "pending_approvals_digest_pending_uidx",
    } <= indexes
    conn.close()


def test_migration_idempotent_rerun_preserves_rows(tmp_path: Path) -> None:
    db = tmp_path / "idem.db"
    _build_legacy_db(db)

    conn = _connect(db)
    pid_a = _insert_row(conn, status="pending", action_digest="dig-a")
    pid_b = _insert_row(conn, status="approved")
    conn.close()

    # Primera migración + una fila 'expired' (solo posible tras migrar).
    conn = _connect(db)
    ensure_capabilities_schema(conn)
    pid_c = _insert_row(conn, status="expired", action_digest="dig-c")
    total_before = conn.execute(
        "SELECT COUNT(*) AS n FROM pending_approvals"
    ).fetchone()["n"]
    conn.close()
    assert total_before == 3

    # Re-correr ensure varias veces = no-op (no recrea, no pierde filas).
    for _ in range(3):
        conn = _connect(db)
        ensure_capabilities_schema(conn)
        conn.close()

    conn = _connect(db)
    rows = {
        r["proposal_id"]: r["status"]
        for r in conn.execute(
            "SELECT proposal_id, status FROM pending_approvals"
        ).fetchall()
    }
    total_after = conn.execute(
        "SELECT COUNT(*) AS n FROM pending_approvals"
    ).fetchone()["n"]
    still_admits_expired = "'expired'" in _table_sql(conn)
    conn.close()

    assert total_after == total_before == 3
    assert rows == {pid_a: "pending", pid_b: "approved", pid_c: "expired"}
    assert still_admits_expired


# ── (a) gate.expire() end-to-end: flip a 'expired', fuera de pendientes ─────────


def _make_gate(db_path: Path) -> SqliteApprovalGate:
    """Gate real sin MFA (expire()/register_pending no la usan)."""
    signing_key = os.urandom(32)
    return SqliteApprovalGate(
        db_path=db_path,
        minter=HitlApprovalMinter(signing_key=signing_key),
        signer=AuditHashChainSigner(signing_key=signing_key),
    )


async def _register(gate: SqliteApprovalGate, pid) -> None:
    await gate.register_pending(
        proposal_id=pid,
        work_item_id=uuid4(),
        consent_context=ConsentContext(tenant_id=uuid4(), operator_id=uuid4()),
        risk=RiskLevel.HIGH,
        justification="ghost card regression",
        parameters_redacted={"path": "/tmp/out.txt"},
        tool_name="fs_write",
        action_digest="dig-expire",
    )


@pytest.mark.asyncio
async def test_expire_flips_pending_to_expired_no_integrity_error(tmp_path: Path) -> None:
    db = tmp_path / "approvals.db"
    gate = _make_gate(db)
    pid = uuid4()
    await _register(gate, pid)

    conn = _connect(db)
    assert _pending_count(conn) == 1
    conn.close()

    # ANTES del fix esto lanzaba sqlite3.IntegrityError (CHECK sin 'expired').
    await gate.expire(proposal_id=pid)

    conn = _connect(db)
    status = conn.execute(
        "SELECT status FROM pending_approvals WHERE proposal_id=?", (str(pid),)
    ).fetchone()["status"]
    pending_after = _pending_count(conn)
    conn.close()

    assert status == "expired"
    assert pending_after == 0  # fuera de la lista de tarjetas pendientes


@pytest.mark.asyncio
async def test_expire_on_legacy_migrated_db_via_gate(tmp_path: Path) -> None:
    # Fila 'pending' en una DB legacy; construir el gate MIGRA el esquema; expire()
    # debe flip-earla a 'expired' sin lanzar (end-to-end legacy→migración→expire).
    db = tmp_path / "legacy_gate.db"
    _build_legacy_db(db)
    pid = uuid4()
    conn = _connect(db)
    _insert_row(conn, proposal_id=str(pid), status="pending")
    conn.close()

    gate = _make_gate(db)  # _ensure_schema → migra el CHECK
    await gate.expire(proposal_id=pid)

    conn = _connect(db)
    status = conn.execute(
        "SELECT status FROM pending_approvals WHERE proposal_id=?", (str(pid),)
    ).fetchone()["status"]
    conn.close()
    assert status == "expired"
