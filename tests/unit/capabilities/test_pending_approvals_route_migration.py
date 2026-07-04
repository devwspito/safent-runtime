"""Unit — EXPAND migration de `route`/`sensitivity`/`agent_id` en `pending_approvals`
(Fase 2 Phase 4b, Enterprise remote approval).

Cubre:
  (a) una DB fresca nace con las 3 columnas (vía ALTER, idempotente).
  (b) una DB legacy (esquema previo a Fase 2 Phase 4a, sin ninguna de las 3
      columnas ni el CHECK 'expired') migra sin perder filas — el path de
      recreación de `status` (que copia por-nombre) también debe copiar las 3
      columnas nuevas.
  (c) re-correr ensure_capabilities_schema varias veces es no-op (no duplica
      columnas, no lanza).
  (d) SqliteApprovalGate.register_pending persiste route/sensitivity/agent_id
      correctamente end-to-end sobre una DB migrada.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.capabilities.infrastructure.schema import ensure_capabilities_schema

pytestmark = pytest.mark.unit

# DB "legacy": ni el CHECK con 'expired' ni las columnas route/sensitivity/
# agent_id existen — mimetiza una DB pre-Fase-2-Phase-4a.
_LEGACY_DDL = """
CREATE TABLE pending_approvals (
    proposal_id          TEXT PRIMARY KEY,
    work_item_id         TEXT NOT NULL,
    tenant_id            TEXT,
    operator_id          TEXT NOT NULL,
    risk                 TEXT NOT NULL CHECK (risk IN ('low','high')),
    tool_name            TEXT,
    action_digest        TEXT,
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
    resolved_at          TEXT,
    conversation_id      TEXT,
    attempt_count        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX pending_approvals_work_item_idx ON pending_approvals (work_item_id);
"""

_NOW = "2026-07-04T12:00:00.000Z"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _build_legacy_db(path: Path) -> None:
    conn = _connect(path)
    conn.executescript(_LEGACY_DDL)
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


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("PRAGMA table_info(pending_approvals)")}


# ── (a) DB fresca ────────────────────────────────────────────────────────────


def test_fresh_db_has_route_sensitivity_agent_id_columns(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    conn = _connect(db)
    ensure_capabilities_schema(conn)
    cols = _columns(conn)
    conn.close()
    assert {"route", "sensitivity", "agent_id"} <= cols


def test_fresh_db_accepts_route_and_sensitivity_values(tmp_path: Path) -> None:
    db = tmp_path / "fresh2.db"
    conn = _connect(db)
    ensure_capabilities_schema(conn)
    pid = _insert_row(conn, route="enterprise", sensitivity='["pii_read"]', agent_id="agent-a")
    row = conn.execute(
        "SELECT route, sensitivity, agent_id FROM pending_approvals WHERE proposal_id=?",
        (pid,),
    ).fetchone()
    conn.close()
    assert row["route"] == "enterprise"
    assert row["sensitivity"] == '["pii_read"]'
    assert row["agent_id"] == "agent-a"


def test_fresh_db_defaults_route_sensitivity_agent_id_to_null(tmp_path: Path) -> None:
    db = tmp_path / "fresh3.db"
    conn = _connect(db)
    ensure_capabilities_schema(conn)
    pid = _insert_row(conn)
    row = conn.execute(
        "SELECT route, sensitivity, agent_id FROM pending_approvals WHERE proposal_id=?",
        (pid,),
    ).fetchone()
    conn.close()
    assert row["route"] is None
    assert row["sensitivity"] is None
    assert row["agent_id"] is None


# ── (b) DB legacy: recreación de status preserva las columnas nuevas ────────


def test_legacy_db_migration_adds_columns_and_preserves_rows(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _build_legacy_db(db)

    conn = _connect(db)
    assert "route" not in _columns(conn)
    pid = _insert_row(conn, status="pending", tool_name="cronjob", action_digest="dig-1")
    conn.close()

    conn = _connect(db)
    ensure_capabilities_schema(conn)
    cols = _columns(conn)
    assert {"route", "sensitivity", "agent_id"} <= cols

    row = conn.execute(
        "SELECT proposal_id, tool_name, action_digest, route, sensitivity, agent_id "
        "FROM pending_approvals WHERE proposal_id=?",
        (pid,),
    ).fetchone()
    conn.close()

    assert row["tool_name"] == "cronjob"
    assert row["action_digest"] == "dig-1"
    assert row["route"] is None  # legacy row never had a route — NULL, not lost


def test_legacy_db_migration_then_new_enterprise_row(tmp_path: Path) -> None:
    """After migrating a legacy DB, a FRESH row can carry route='enterprise'."""
    db = tmp_path / "legacy2.db"
    _build_legacy_db(db)
    conn = _connect(db)
    _insert_row(conn)
    conn.close()

    conn = _connect(db)
    ensure_capabilities_schema(conn)
    pid = _insert_row(
        conn, route="enterprise", sensitivity='["new_egress"]', agent_id="ceo-bot"
    )
    row = conn.execute(
        "SELECT route, sensitivity, agent_id FROM pending_approvals WHERE proposal_id=?",
        (pid,),
    ).fetchone()
    conn.close()

    assert row["route"] == "enterprise"
    assert row["sensitivity"] == '["new_egress"]'
    assert row["agent_id"] == "ceo-bot"


# ── (c) idempotencia ────────────────────────────────────────────────────────


def test_migration_idempotent_rerun_preserves_rows_and_columns(tmp_path: Path) -> None:
    db = tmp_path / "idem.db"
    _build_legacy_db(db)
    conn = _connect(db)
    pid = _insert_row(conn)  # legacy schema — no route/sensitivity/agent_id yet
    conn.close()

    for _ in range(3):
        conn = _connect(db)
        ensure_capabilities_schema(conn)
        conn.close()

    conn = _connect(db)
    total = conn.execute("SELECT COUNT(*) AS n FROM pending_approvals").fetchone()["n"]
    cols = _columns(conn)
    conn.close()

    assert total == 1
    assert {"route", "sensitivity", "agent_id"} <= cols


# ── (d) end-to-end vía SqliteApprovalGate ───────────────────────────────────


@pytest.mark.asyncio
async def test_gate_register_pending_persists_new_columns_on_migrated_legacy_db(
    tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
    from hermes.capabilities.domain.ports import ConsentContext, RiskLevel
    from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate
    from hermes.capabilities.tool_sensitivity import SensitivityCategory

    db = tmp_path / "legacy_gate.db"
    _build_legacy_db(db)

    signer = MagicMock()
    signer.append = MagicMock()
    signer.append_and_persist = AsyncMock()
    gate = SqliteApprovalGate(
        db_path=db,
        minter=HitlApprovalMinter(signing_key=b"k" * 32),
        signer=signer,
        audit_repo=None,
        mfa_verifier=None,
    )  # constructing the gate migrates the schema

    proposal_id = uuid4()
    await gate.register_pending(
        proposal_id=proposal_id,
        work_item_id=uuid4(),
        consent_context=ConsentContext(tenant_id=uuid4(), operator_id=uuid4()),
        risk=RiskLevel.HIGH,
        justification="migration e2e",
        parameters_redacted={},
        tool_name="cronjob",
        action_digest="dig-e2e",
        route="enterprise",
        sensitivity_categories=frozenset({SensitivityCategory.PII_READ, SensitivityCategory.SPEND}),
        agent_id="agent-e2e",
    )

    conn = _connect(db)
    row = conn.execute(
        "SELECT route, sensitivity, agent_id FROM pending_approvals WHERE proposal_id=?",
        (str(proposal_id),),
    ).fetchone()
    conn.close()

    assert row["route"] == "enterprise"
    assert sorted(__import__("json").loads(row["sensitivity"])) == ["pii_read", "spend"]
    assert row["agent_id"] == "agent-e2e"
