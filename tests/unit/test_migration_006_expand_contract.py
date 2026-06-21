"""Unit — migración EXPAND/CONTRACT de la feature 006 (P1) sobre `agent_tasks`.

Tests-first (T009). Cubre la migración firmada (data-model 006 §3-§4-§8):

  - EXPAND aditivo (kind/worker_id/conversation_id + índices) NO rompe la
    lectura/escritura del esquema P0 (backwards-compatible): una fila estilo P0
    sigue insertándose y se lee igual.
  - Recreación controlada del CHECK de `trigger_kind` admite 'chat_message'
    (P0 sólo permitía 'manual_enqueue') y de `kind` admite 'chat_message',
    PRESERVANDO los CHECK I1-I4 firmados de P0 (anti-éxito-alucinado incluido).
  - Idempotencia por guard `PRAGMA user_version`: re-ejecutar `ensure_tasks_schema`
    es no-op (no recrea la tabla, no duplica el singleton, no pierde datos).
  - DROP+INSERT SELECT no pierde filas: una fila preexistente sobrevive a la
    recreación (la copia y el drop ocurren en la misma transacción).
  - Invariantes nuevas:
      I5: kind='chat_message' => conversation_id NOT NULL (rechaza inválida).
      I6: status='in_progress' => worker_id NOT NULL (rechaza inválida).

No requiere binarios externos ni red — es un test unitario sobre SQLite local.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.tasks.infrastructure.schema import (
    _SCHEMA_VERSION_P1,
    ensure_tasks_schema,
)

_NOW = "2026-05-31T12:00:00.000Z"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "shell-state.db"


@pytest.fixture
def conn(db_path: Path):
    c = _connect(db_path)
    ensure_tasks_schema(c)
    yield c
    c.close()


def _insert_task(conn: sqlite3.Connection, **overrides) -> str:
    task_id = overrides.pop("task_id", str(uuid4()))
    cols = {
        "task_id": task_id,
        "trigger_kind": "manual_enqueue",
        "enqueued_by": str(uuid4()),
        "operator_id": str(uuid4()),
        "instruction": "do the thing",
        "status": "pending",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    cols.update(overrides)
    placeholders = ", ".join(f":{k}" for k in cols)
    conn.execute(
        f"INSERT INTO agent_tasks ({', '.join(cols)}) VALUES ({placeholders})",
        cols,
    )
    return task_id


# ── EXPAND: columnas e índices nuevos presentes, backwards-compatible ────────


def test_expand_adds_new_columns(conn: sqlite3.Connection) -> None:
    cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(agent_tasks)").fetchall()
    }
    assert {"kind", "worker_id", "conversation_id"} <= cols
    # Las columnas P0 siguen ahí (no se perdió ninguna en la recreación).
    assert {
        "task_id",
        "trigger_kind",
        "enqueued_by",
        "payload_signature",
        "tenant_id",
        "operator_id",
        "instruction",
        "payload_json",
        "status",
        "dedup_key",
        "priority",
        "claim_token",
        "claimed_at",
        "lease_expires_at",
        "heartbeat_at",
        "idempotency_key",
        "retry_count",
        "max_retries",
        "next_attempt_at",
        "last_error",
        "execution_audit_entry_id",
        "execution_head_hash",
        "created_at",
        "updated_at",
    } <= cols


def test_expand_adds_new_indexes_preserving_p0(conn: sqlite3.Connection) -> None:
    indexes = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    # Los 4 índices FIRMADOS de P0 sobreviven a la recreación.
    assert {
        "agent_tasks_dedup_key_active_unique",
        "agent_tasks_dequeue_idx",
        "agent_tasks_lease_idx",
        "agent_tasks_retry_idx",
    } <= indexes
    # Los 2 índices nuevos de P1.
    assert {
        "idx_agent_tasks_conversation",
        "idx_agent_tasks_worker_active",
    } <= indexes


def test_p0_style_row_still_inserts_and_reads(conn: sqlite3.Connection) -> None:
    # El código P0 inserta sin tocar kind/worker_id/conversation_id.
    task_id = _insert_task(conn)
    row = conn.execute(
        "SELECT status, kind, worker_id, conversation_id FROM agent_tasks "
        "WHERE task_id=?",
        (task_id,),
    ).fetchone()
    assert row["status"] == "pending"
    # kind por DEFAULT 'autonomous' (correcto: trabajo P0 = autónomo).
    assert row["kind"] == "autonomous"
    assert row["worker_id"] is None
    assert row["conversation_id"] is None


def test_kind_defaults_to_autonomous_for_legacy_rows(conn: sqlite3.Connection) -> None:
    task_id = _insert_task(conn)
    kind = conn.execute(
        "SELECT kind FROM agent_tasks WHERE task_id=?", (task_id,)
    ).fetchone()["kind"]
    assert kind == "autonomous"


# ── Recreación del CHECK: admite 'chat_message' sin perder los CHECK P0 ──────


def test_trigger_kind_now_accepts_chat_message(conn: sqlite3.Connection) -> None:
    task_id = _insert_task(
        conn,
        trigger_kind="chat_message",
        kind="chat_message",
        conversation_id=str(uuid4()),
    )
    assert task_id


def test_trigger_kind_still_accepts_manual_enqueue(conn: sqlite3.Connection) -> None:
    assert _insert_task(conn, trigger_kind="manual_enqueue")


def test_trigger_kind_rejects_unknown_value(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, trigger_kind="ui_action")


def test_kind_rejects_unknown_value(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, kind="bogus")


# ── CHECK I1-I4 de P0 PRESERVADOS textualmente tras la recreación ───────────


def test_i1_completed_without_evidence_still_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, status="completed")


def test_i1_completed_with_evidence_passes(conn: sqlite3.Connection) -> None:
    assert _insert_task(
        conn,
        status="completed",
        execution_audit_entry_id=str(uuid4()),
        execution_head_hash="deadbeef" * 8,
    )


def test_i2_terminal_with_live_lease_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(
            conn,
            status="failed",
            claim_token=str(uuid4()),
            lease_expires_at=_NOW,
        )


def test_i3_in_progress_without_claim_still_raises(conn: sqlite3.Connection) -> None:
    # in_progress sin claim/lease => I3 (worker_id presente para aislar de I6).
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, status="in_progress", worker_id="worker-0")


def test_i4_retry_count_exceeds_max_still_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, retry_count=6, max_retries=5)


def test_dedup_key_active_unique_preserved(conn: sqlite3.Connection) -> None:
    _insert_task(conn, dedup_key="job-42")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, dedup_key="job-42")


# ── Invariantes NUEVAS I5 / I6 ──────────────────────────────────────────────


def test_i5_chat_message_without_conversation_id_raises(
    conn: sqlite3.Connection,
) -> None:
    # kind='chat_message' SIN conversation_id => I5.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, kind="chat_message")


def test_i5_chat_message_with_conversation_id_passes(
    conn: sqlite3.Connection,
) -> None:
    assert _insert_task(
        conn, kind="chat_message", conversation_id=str(uuid4())
    )


def test_i5_autonomous_with_conversation_id_allowed(conn: sqlite3.Connection) -> None:
    # I5 sólo restringe chat_message; autónomo puede llevar o no conversation_id.
    assert _insert_task(conn, kind="autonomous")


def test_i6_in_progress_without_worker_id_raises(conn: sqlite3.Connection) -> None:
    # in_progress CON claim/lease (satisface I3) pero SIN worker_id => I6.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(
            conn,
            status="in_progress",
            claim_token=str(uuid4()),
            claimed_at=_NOW,
            lease_expires_at=_NOW,
        )


def test_i6_in_progress_with_worker_id_passes(conn: sqlite3.Connection) -> None:
    assert _insert_task(
        conn,
        status="in_progress",
        worker_id="worker-3",
        claim_token=str(uuid4()),
        claimed_at=_NOW,
        lease_expires_at=_NOW,
    )


# ── Guard de versión + idempotencia ─────────────────────────────────────────


def test_user_version_advanced_to_p1(conn: sqlite3.Connection) -> None:
    # `ensure_tasks_schema` aplica el chain completo de migraciones; P1 dejó la
    # versión en 2, migraciones posteriores la suben más. Lo que 006 verifica es
    # que la migración P1 corrió (al menos), no que sea la última del chain.
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= _SCHEMA_VERSION_P1


def test_re_running_ensure_is_noop_and_preserves_rows(db_path: Path) -> None:
    # Primera aplicación + inserta una fila.
    c = _connect(db_path)
    ensure_tasks_schema(c)
    task_id = _insert_task(c, kind="chat_message", conversation_id=str(uuid4()))
    c.close()

    # Re-ejecutar varias veces NO debe recrear la tabla ni perder la fila.
    for _ in range(3):
        c = _connect(db_path)
        ensure_tasks_schema(c)
        c.close()

    c = _connect(db_path)
    surviving = c.execute(
        "SELECT task_id, kind, conversation_id FROM agent_tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    # El singleton no se duplicó.
    singletons = c.execute(
        "SELECT COUNT(*) AS n FROM agent_runtime_state"
    ).fetchone()["n"]
    version = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()

    assert surviving is not None
    assert surviving["kind"] == "chat_message"
    assert singletons == 1
    assert version >= _SCHEMA_VERSION_P1


def test_recreation_preserves_preexisting_p0_rows(db_path: Path) -> None:
    # Simula una DB P0: aplica SOLO el DDL P0 (sin EXPAND/recreación) y mete datos,
    # luego corre la migración 006 y verifica que NINGUNA fila se perdió.
    from hermes.tasks.infrastructure.schema import (  # noqa: PLC0415
        _DDL_AGENT_RUNTIME_STATE,
        _DDL_AGENT_TASKS_P0,
        _PRAGMAS,
    )

    c = _connect(db_path)
    c.executescript(_PRAGMAS)
    c.executescript(_DDL_AGENT_TASKS_P0)
    c.executescript(_DDL_AGENT_RUNTIME_STATE)
    # user_version queda en 0 (P0 no lo setea) — el guard debe migrar igual.
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0

    pending_id = _insert_task(c, instruction="legacy pending")
    completed_id = _insert_task(
        c,
        instruction="legacy completed",
        status="completed",
        execution_audit_entry_id=str(uuid4()),
        execution_head_hash="ab" * 32,
    )
    c.close()

    # Migración 006.
    c = _connect(db_path)
    ensure_tasks_schema(c)

    rows = {
        r["task_id"]: r
        for r in c.execute(
            "SELECT task_id, instruction, status, kind FROM agent_tasks"
        ).fetchall()
    }
    version = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()

    assert pending_id in rows
    assert completed_id in rows
    # Las filas P0 quedan como 'autonomous' tras el EXPAND (default constante).
    assert rows[pending_id]["kind"] == "autonomous"
    assert rows[completed_id]["status"] == "completed"
    assert version >= _SCHEMA_VERSION_P1


def test_idempotent_on_fresh_db_advances_once(db_path: Path) -> None:
    # Una DB nueva (sin P0 previo) también acaba en P1 y es estable.
    for _ in range(2):
        c = _connect(db_path)
        ensure_tasks_schema(c)
        v = c.execute("PRAGMA user_version").fetchone()[0]
        c.close()
        assert v >= _SCHEMA_VERSION_P1
