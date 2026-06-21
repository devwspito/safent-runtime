"""Unit — esquema `execution_contexts` (PIEZA 4 / feature 006, T012).

Verifica que `ensure_execution_contexts_schema` crea la tabla, los CHECK y los
índices firmados (data-model 006 §3.2 / §8) y que se comportan:

  - tabla + índices presentes (UNIQUE parcial + lease + task).
  - fila 'claimed' válida pasa.
  - taxonomía `input_surface` e `input_owner` aplican whitelist (CHECK).
  - UNIQUE parcial: dos 'claimed' de la misma (input_surface, isolation_key)
    chocan (fail-closed, FR-021/FR-022); pero tras 'released' se re-reclama.
  - I7: 'released' con lease vivo rechazado.
  - I8: 'claimed' sin owning_worker_id rechazado.
  - FK a agent_tasks: ON DELETE SET NULL.
  - idempotencia: re-aplicar no destruye ni falla.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.execution.infrastructure.sqlite_execution_context_store import (
    SqliteExecutionContextStore,
    ensure_execution_contexts_schema,
)
from hermes.tasks.infrastructure.schema import ensure_tasks_schema

_NOW = "2026-05-31T12:00:00.000Z"
_LATER = "2026-05-31T13:00:00.000Z"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "shell-state.db"
    c = _connect(db_path)
    ensure_tasks_schema(c)  # provee agent_tasks (FK target)
    ensure_execution_contexts_schema(c)
    yield c
    c.close()


def _insert_ctx(conn: sqlite3.Connection, **overrides) -> str:
    context_id = overrides.pop("context_id", str(uuid4()))
    cols = {
        "context_id": context_id,
        "input_surface": "browser",
        "isolation_key": "tenant-a:site-x",
        "input_owner": "agent",
        "owning_worker_id": "worker-0",
        "status": "claimed",
        "claimed_at": _NOW,
        "lease_expires_at": _LATER,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    cols.update(overrides)
    placeholders = ", ".join(f":{k}" for k in cols)
    conn.execute(
        f"INSERT INTO execution_contexts ({', '.join(cols)}) "
        f"VALUES ({placeholders})",
        cols,
    )
    return context_id


# ── Presencia de esquema ────────────────────────────────────────────────────


def test_table_and_indexes_exist(conn: sqlite3.Connection) -> None:
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "execution_contexts" in tables

    indexes = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert {
        "execution_contexts_surface_owner_unique",
        "idx_execution_contexts_lease",
        "idx_execution_contexts_task",
    } <= indexes


def test_store_creates_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    # Necesita agent_tasks para la FK; crea ambos.
    c = _connect(db_path)
    ensure_tasks_schema(c)
    c.close()
    SqliteExecutionContextStore(db_path=db_path)
    c = _connect(db_path)
    tables = {
        r["name"]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    c.close()
    assert "execution_contexts" in tables


def test_schema_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "idem.db"
    for _ in range(3):
        c = _connect(db_path)
        ensure_tasks_schema(c)
        ensure_execution_contexts_schema(c)
        c.close()
    c = _connect(db_path)
    n = c.execute("SELECT COUNT(*) AS n FROM execution_contexts").fetchone()["n"]
    c.close()
    assert n == 0


# ── Fila válida + whitelists ────────────────────────────────────────────────


def test_valid_claimed_context_inserts(conn: sqlite3.Connection) -> None:
    context_id = _insert_ctx(conn)
    row = conn.execute(
        "SELECT status, input_surface, input_owner FROM execution_contexts "
        "WHERE context_id=?",
        (context_id,),
    ).fetchone()
    assert row["status"] == "claimed"
    assert row["input_surface"] == "browser"
    assert row["input_owner"] == "agent"


def test_input_surface_whitelist_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ctx(conn, input_surface="webcam")


def test_input_owner_whitelist_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ctx(conn, input_owner="root")


def test_status_whitelist_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ctx(conn, status="pending")


# ── UNIQUE parcial: un dueño por superficie (fail-closed) ───────────────────


def test_one_owner_per_surface_fail_closed(conn: sqlite3.Connection) -> None:
    _insert_ctx(conn, input_surface="keyboard", isolation_key="seat0")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ctx(conn, input_surface="keyboard", isolation_key="seat0")


def test_different_isolation_keys_do_not_collide(conn: sqlite3.Connection) -> None:
    _insert_ctx(conn, input_surface="browser", isolation_key="sess-1")
    assert _insert_ctx(conn, input_surface="browser", isolation_key="sess-2")


def test_released_frees_surface_for_reclaim(conn: sqlite3.Connection) -> None:
    # Primer claim, luego release => la misma superficie se re-reclama.
    _insert_ctx(
        conn,
        input_surface="mouse",
        isolation_key="seat0",
        status="released",
        lease_expires_at=None,
        released_at=_LATER,
    )
    assert _insert_ctx(conn, input_surface="mouse", isolation_key="seat0")


# ── I7 / I8 ─────────────────────────────────────────────────────────────────


def test_i7_released_with_live_lease_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ctx(
            conn,
            status="released",
            lease_expires_at=_LATER,  # released NO debe llevar lease vivo
            released_at=_LATER,
        )


def test_i8_claimed_without_worker_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ctx(conn, owning_worker_id=None)


# ── FK a agent_tasks ────────────────────────────────────────────────────────


def test_fk_on_delete_sets_null(conn: sqlite3.Connection) -> None:
    task_id = str(uuid4())
    conn.execute(
        "INSERT INTO agent_tasks "
        "(task_id, trigger_kind, enqueued_by, operator_id, instruction, "
        " status, created_at, updated_at) "
        "VALUES (?, 'manual_enqueue', ?, ?, 'x', 'pending', ?, ?)",
        (task_id, str(uuid4()), str(uuid4()), _NOW, _NOW),
    )
    ctx_id = _insert_ctx(conn, owning_task_id=task_id)
    conn.execute("DELETE FROM agent_tasks WHERE task_id=?", (task_id,))
    owner = conn.execute(
        "SELECT owning_task_id FROM execution_contexts WHERE context_id=?",
        (ctx_id,),
    ).fetchone()["owning_task_id"]
    assert owner is None
