"""Integration — esquema SQLite firmado de la feature 005 (T007-T010).

Abre una DB SQLite temporal, aplica las TRES funciones `ensure_*` del esquema y
verifica que tablas, índices, CHECK y singleton existen y se comportan:

  - agent_tasks         (T007): fila válida pasa; I1 (completed sin evidencia) y
                         I3 (in_progress sin claim/lease) e I4 (contadores)
                         lanzan IntegrityError; dedup_key activa duplicada lanza.
  - agent_runtime_state (T008): singleton sembrado 'running'; segundo INSERT
                         'singleton' es no-op; otro id viola el CHECK.
  - pending_approvals   (T009): fila válida; risk/status fuera de whitelist lanzan.
  - audit_chain_entries (T010): append-only ordenado por seq; head = mayor seq;
                         entry_id único; coexiste con audit_entries_view.

Idempotencia: re-ejecutar las tres `ensure_*` no destruye ni falla.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.agents_os.infrastructure.audit_schema import ensure_audit_chain_schema
from hermes.capabilities.infrastructure.schema import ensure_capabilities_schema
from hermes.tasks.infrastructure.schema import ensure_tasks_schema

pytestmark = pytest.mark.integration

_NOW = "2026-05-31T12:00:00.000Z"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "shell-state.db"
    c = _connect(db_path)
    # Aplica las tres veces para probar idempotencia desde el primer uso.
    ensure_tasks_schema(c)
    ensure_capabilities_schema(c)
    ensure_audit_chain_schema(c)
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


# ── Schema presence ─────────────────────────────────────────────────────────


def test_all_tables_and_indexes_exist(conn: sqlite3.Connection) -> None:
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "agent_tasks",
        "agent_runtime_state",
        "pending_approvals",
        "audit_chain_entries",
        "audit_entries_view",
    } <= tables

    indexes = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert {
        "agent_tasks_dedup_key_active_unique",
        "agent_tasks_dequeue_idx",
        "agent_tasks_lease_idx",
        "agent_tasks_retry_idx",
        "pending_approvals_work_item_idx",
        "audit_chain_entries_seq_idx",
        "audit_chain_entries_tenant_seq_idx",
    } <= indexes


def test_ensure_functions_are_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "idem.db"
    for _ in range(3):
        c = _connect(db_path)
        ensure_tasks_schema(c)
        ensure_capabilities_schema(c)
        ensure_audit_chain_schema(c)
        c.close()
    c = _connect(db_path)
    # Singleton no se duplicó pese a tres ensure_tasks_schema.
    count = c.execute("SELECT COUNT(*) AS n FROM agent_runtime_state").fetchone()["n"]
    assert count == 1
    c.close()


# ── agent_tasks ─────────────────────────────────────────────────────────────


def test_valid_pending_task_inserts(conn: sqlite3.Connection) -> None:
    task_id = _insert_task(conn)
    row = conn.execute(
        "SELECT status, retry_count, max_retries FROM agent_tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    assert row["status"] == "pending"
    assert row["retry_count"] == 0
    assert row["max_retries"] == 5


def test_i1_completed_without_evidence_raises(conn: sqlite3.Connection) -> None:
    # 'completed' sin execution_audit_entry_id+execution_head_hash => I1.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, status="completed")


def test_i1_completed_with_evidence_passes(conn: sqlite3.Connection) -> None:
    task_id = _insert_task(
        conn,
        status="completed",
        execution_audit_entry_id=str(uuid4()),
        execution_head_hash="deadbeef" * 8,
    )
    assert task_id


def test_i3_in_progress_without_claim_raises(conn: sqlite3.Connection) -> None:
    # 'in_progress' sin claim_token/claimed_at/lease_expires_at => I3.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, status="in_progress")


def test_i4_retry_count_exceeds_max_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, retry_count=6, max_retries=5)


def test_trigger_kind_whitelist_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, trigger_kind="ui_action")


def test_status_whitelist_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, status="bogus")


def test_dedup_key_active_unique_raises_on_duplicate(conn: sqlite3.Connection) -> None:
    _insert_task(conn, dedup_key="job-42")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, dedup_key="job-42")  # otra fila viva, misma dedup_key


def test_dedup_key_reusable_after_terminal(conn: sqlite3.Connection) -> None:
    # Una dedup_key en fila terminal NO bloquea una nueva fila viva (índice parcial).
    _insert_task(
        conn,
        dedup_key="job-7",
        status="completed",
        execution_audit_entry_id=str(uuid4()),
        execution_head_hash="ab" * 32,
    )
    second = _insert_task(conn, dedup_key="job-7")  # status pending => permitido
    assert second


# ── agent_runtime_state ─────────────────────────────────────────────────────


def test_runtime_state_singleton_seeded_running(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT id, loop_state FROM agent_runtime_state"
    ).fetchone()
    assert row["id"] == "singleton"
    assert row["loop_state"] == "running"


def test_runtime_state_rejects_non_singleton_id(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO agent_runtime_state (id, loop_state, updated_at) "
            "VALUES ('other', 'running', ?)",
            (_NOW,),
        )


def test_runtime_state_rejects_unknown_loop_state(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE agent_runtime_state SET loop_state='halted' WHERE id='singleton'"
        )


# ── pending_approvals ───────────────────────────────────────────────────────


def test_pending_approval_valid_inserts(conn: sqlite3.Connection) -> None:
    proposal_id = str(uuid4())
    conn.execute(
        "INSERT INTO pending_approvals "
        "(proposal_id, work_item_id, operator_id, risk, created_at) "
        "VALUES (?, ?, ?, 'high', ?)",
        (proposal_id, str(uuid4()), str(uuid4()), _NOW),
    )
    row = conn.execute(
        "SELECT status, parameters_redacted FROM pending_approvals WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    assert row["status"] == "pending"
    assert row["parameters_redacted"] == "{}"


def test_pending_approval_rejects_unknown_risk(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pending_approvals "
            "(proposal_id, work_item_id, operator_id, risk, created_at) "
            "VALUES (?, ?, ?, 'critical', ?)",
            (str(uuid4()), str(uuid4()), str(uuid4()), _NOW),
        )


def test_pending_approval_rejects_unknown_status(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pending_approvals "
            "(proposal_id, work_item_id, operator_id, risk, status, created_at) "
            "VALUES (?, ?, ?, 'low', 'consumed', ?)",
            (str(uuid4()), str(uuid4()), str(uuid4()), _NOW),
        )


# ── audit_chain_entries ─────────────────────────────────────────────────────


def _append_audit(conn: sqlite3.Connection, **overrides) -> str:
    entry_id = overrides.pop("entry_id", str(uuid4()))
    cols = {
        "entry_id": entry_id,
        "timestamp": _NOW,
        "actor": "agent-loop",
        "audit_kind": "task_claimed",
        "payload_hash_hex": "11" * 32,
        "prev_entry_hash_hex": "00" * 32,
        "signed_payload_hash_hex": overrides.pop("signed", "22" * 32),
        "signature_hex": "33" * 32,
        "created_at": _NOW,
    }
    cols.update(overrides)
    placeholders = ", ".join(f":{k}" for k in cols)
    conn.execute(
        f"INSERT INTO audit_chain_entries ({', '.join(cols)}) VALUES ({placeholders})",
        cols,
    )
    return entry_id


def test_audit_chain_append_orders_by_seq_and_head(conn: sqlite3.Connection) -> None:
    _append_audit(conn, signed="aa" * 32)
    _append_audit(conn, signed="bb" * 32)
    head = conn.execute(
        "SELECT signed_payload_hash_hex FROM audit_chain_entries "
        "ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert head["signed_payload_hash_hex"] == "bb" * 32

    seqs = [
        r["seq"]
        for r in conn.execute(
            "SELECT seq FROM audit_chain_entries ORDER BY seq ASC"
        ).fetchall()
    ]
    assert seqs == sorted(seqs)
    assert len(seqs) == 2


def test_audit_chain_entry_id_is_unique(conn: sqlite3.Connection) -> None:
    eid = _append_audit(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _append_audit(conn, entry_id=eid)
