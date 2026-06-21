"""Integration — migración de triggers default-deny de la feature 007 (P2).

Tests-first (GATE común P2). Cubre el data-model 007 §3-§4-§9:

  - DOS tablas nuevas existen tras `ensure_tasks_schema`:
      * authorized_trigger_types — catálogo enum sembrado (3 tipos), con
        enabled_by_default=0 en TODAS las filas (DEFAULT-DENY a nivel de fila,
        CHECK lo fija a 0).
      * authorized_trigger_instances — la allow-list FIRMADA, NACE VACÍA
        (0 filas → 0 auto-disparos de fábrica, SC-013 / I14).
  - agent_tasks admite los 3 trigger_kind nuevos (timer / system_event /
    self_enqueue) preservando TEXTUALMENTE I1-I6 firmados de P0/P1 y los
    invariantes de execution_contexts I7-I8 (P1, store).
  - I10 (nuevo): trigger_kind ∈ auto ⇒ trigger_instance_id NOT NULL;
    trigger_kind ∈ manual/chat ⇒ trigger_instance_id IS NULL.
  - I11 (nuevo): enabled=0 ⇔ revoked_at NOT NULL (coherencia revocación).
  - `kind` NO se recrea (OQ-2): una tarea auto-disparada es kind='autonomous'
    con trigger_kind específico.
  - Guard `PRAGMA user_version` 2→3 idempotente: re-ejecutar es no-op, no recrea
    la tabla, no pierde filas, no re-siembra el catálogo.
  - El esquema P0/P1 sigue verde: columnas, índices y CHECK I1-I6 intactos.

No requiere binarios externos ni red — DB SQLite local. Marcado @integration
porque toca disco (fichero shell-state.db temporal), igual que test_schema_005.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.tasks.infrastructure.schema import (
    _SCHEMA_VERSION_P1,
    _SCHEMA_VERSION_P2,
    ensure_tasks_schema,
)

pytestmark = pytest.mark.integration

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


def _authorize_trigger(conn: sqlite3.Connection, **overrides) -> str:
    """Inserta una instancia de origen autorizada (firma de admin simulada)."""
    instance_id = overrides.pop("instance_id", str(uuid4()))
    cols = {
        "instance_id": instance_id,
        "trigger_type": "timer",
        "scope_value": "*-*-* 09:00:00",
        "created_by_admin_uuid": str(uuid4()),
        "authorized_at": _NOW,
        "approval_signature": "ab" * 32,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    cols.update(overrides)
    placeholders = ", ".join(f":{k}" for k in cols)
    conn.execute(
        f"INSERT INTO authorized_trigger_instances ({', '.join(cols)}) "
        f"VALUES ({placeholders})",
        cols,
    )
    return instance_id


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


# ── Las DOS tablas nuevas existen ───────────────────────────────────────────


def test_authorized_trigger_tables_exist(conn: sqlite3.Connection) -> None:
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"authorized_trigger_types", "authorized_trigger_instances"} <= tables


def test_authorized_trigger_instances_indexes_exist(conn: sqlite3.Connection) -> None:
    indexes = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert {
        "idx_authorized_trigger_instances_lookup",
        "idx_authorized_trigger_instances_type",
        "idx_agent_tasks_trigger_instance",
    } <= indexes


# ── Catálogo de tipos: sembrado, enabled_by_default=0 (DEFAULT-DENY) ─────────


def test_trigger_types_catalog_seeded(conn: sqlite3.Connection) -> None:
    types = {
        r["trigger_type"]
        for r in conn.execute(
            "SELECT trigger_type FROM authorized_trigger_types"
        ).fetchall()
    }
    assert types == {"timer", "system_event", "self_enqueue"}


def test_every_trigger_type_is_default_deny(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT enabled_by_default FROM authorized_trigger_types"
    ).fetchall()
    assert rows  # sembrado
    assert all(r["enabled_by_default"] == 0 for r in rows)


def test_trigger_type_enabled_by_default_one_is_rejected(
    conn: sqlite3.Connection,
) -> None:
    # CHECK (enabled_by_default = 0) hace IMPOSIBLE habilitar un tipo de fábrica.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO authorized_trigger_types "
            "(trigger_type, scope_validation, max_risk_level, "
            " enabled_by_default, created_at, updated_at) "
            "VALUES ('timer','cron_expression','high',1,?,?)",
            (_NOW, _NOW),
        )


def test_trigger_type_rejects_unknown_type(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO authorized_trigger_types "
            "(trigger_type, scope_validation, max_risk_level, created_at, updated_at) "
            "VALUES ('webhook','event_class','high',?,?)",
            (_NOW, _NOW),
        )


# ── La allow-list de instancias NACE VACÍA (SC-013 / I14) ───────────────────


def test_authorized_trigger_instances_starts_empty(conn: sqlite3.Connection) -> None:
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM authorized_trigger_instances"
    ).fetchone()["n"]
    assert count == 0


def test_authorizing_an_instance_persists(conn: sqlite3.Connection) -> None:
    instance_id = _authorize_trigger(conn)
    row = conn.execute(
        "SELECT enabled, revoked_at FROM authorized_trigger_instances "
        "WHERE instance_id=?",
        (instance_id,),
    ).fetchone()
    assert row["enabled"] == 1
    assert row["revoked_at"] is None


def test_instance_requires_catalogued_type_fk(conn: sqlite3.Connection) -> None:
    # FK a authorized_trigger_types: un tipo no catalogado no puede instanciarse.
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        _authorize_trigger(conn, trigger_type="webhook")


# ── I11: enabled=0 ⇔ revoked_at NOT NULL (coherencia de la revocación) ───────


def test_i11_disabled_without_revoked_at_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _authorize_trigger(conn, enabled=0)  # revoked_at None => incoherente


def test_i11_enabled_with_revoked_at_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _authorize_trigger(conn, enabled=1, revoked_at=_NOW)


def test_i11_revoked_instance_is_coherent(conn: sqlite3.Connection) -> None:
    instance_id = _authorize_trigger(
        conn, enabled=0, revoked_at=_NOW, revoked_by_admin_uuid=str(uuid4())
    )
    assert instance_id


# ── trigger_kind: admite los 3 nuevos valores auto ──────────────────────────


def test_trigger_kind_accepts_timer(conn: sqlite3.Connection) -> None:
    instance_id = _authorize_trigger(conn, trigger_type="timer")
    assert _insert_task(
        conn, trigger_kind="timer", trigger_instance_id=instance_id
    )


def test_trigger_kind_accepts_system_event(conn: sqlite3.Connection) -> None:
    instance_id = _authorize_trigger(
        conn, trigger_type="system_event", scope_value="udev.usb_connected"
    )
    assert _insert_task(
        conn, trigger_kind="system_event", trigger_instance_id=instance_id
    )


def test_trigger_kind_accepts_self_enqueue(conn: sqlite3.Connection) -> None:
    instance_id = _authorize_trigger(
        conn,
        trigger_type="self_enqueue",
        scope_value="autonomous",
        risk_ceiling="low",
    )
    assert _insert_task(
        conn, trigger_kind="self_enqueue", trigger_instance_id=instance_id
    )


def test_trigger_kind_still_accepts_p0_p1_values(conn: sqlite3.Connection) -> None:
    assert _insert_task(conn, trigger_kind="manual_enqueue")
    assert _insert_task(
        conn,
        trigger_kind="chat_message",
        kind="chat_message",
        conversation_id=str(uuid4()),
    )


def test_trigger_kind_rejects_unknown_value(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, trigger_kind="webhook")


# ── OQ-2: `kind` NO se recrea — auto-disparadas son kind='autonomous' ───────


def test_auto_task_keeps_kind_autonomous(conn: sqlite3.Connection) -> None:
    instance_id = _authorize_trigger(conn, trigger_type="timer")
    task_id = _insert_task(
        conn, trigger_kind="timer", trigger_instance_id=instance_id
    )
    kind = conn.execute(
        "SELECT kind FROM agent_tasks WHERE task_id=?", (task_id,)
    ).fetchone()["kind"]
    assert kind == "autonomous"


def test_kind_whitelist_unchanged(conn: sqlite3.Connection) -> None:
    # `kind` sigue siendo autonomous|chat_message; no admite valores de origen.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, kind="timer")


# ── I10: atribución obligatoria de origen para auto; NULL para manual/chat ──


def test_i10_auto_without_trigger_instance_raises(conn: sqlite3.Connection) -> None:
    for auto_kind in ("timer", "system_event", "self_enqueue"):
        with pytest.raises(sqlite3.IntegrityError):
            _insert_task(conn, trigger_kind=auto_kind)  # trigger_instance_id None


def test_i10_manual_with_trigger_instance_raises(conn: sqlite3.Connection) -> None:
    instance_id = _authorize_trigger(conn, trigger_type="timer")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(
            conn,
            trigger_kind="manual_enqueue",
            trigger_instance_id=instance_id,
        )


def test_i10_chat_with_trigger_instance_raises(conn: sqlite3.Connection) -> None:
    instance_id = _authorize_trigger(conn, trigger_type="timer")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(
            conn,
            trigger_kind="chat_message",
            kind="chat_message",
            conversation_id=str(uuid4()),
            trigger_instance_id=instance_id,
        )


def test_i10_manual_with_null_instance_passes(conn: sqlite3.Connection) -> None:
    assert _insert_task(conn, trigger_kind="manual_enqueue")


# ── enqueued_by SIEMPRE NOT NULL (I12, preservado de P0) ────────────────────


def test_i12_enqueued_by_not_null_preserved(conn: sqlite3.Connection) -> None:
    instance_id = _authorize_trigger(conn, trigger_type="timer")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO agent_tasks "
            "(task_id, trigger_kind, operator_id, instruction, status, "
            " trigger_instance_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid4()), "timer", str(uuid4()), "x", "pending",
                instance_id, _NOW, _NOW,
            ),
        )


# ── P0/P1 CHECK I1-I6 preservados TEXTUALMENTE tras la recreación P2 ────────


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
            conn, status="failed", claim_token=str(uuid4()), lease_expires_at=_NOW
        )


def test_i3_in_progress_without_claim_still_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, status="in_progress", worker_id="worker-0")


def test_i4_retry_count_exceeds_max_still_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, retry_count=6, max_retries=5)


def test_i5_chat_message_without_conversation_id_raises(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, kind="chat_message")


def test_i6_in_progress_without_worker_id_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(
            conn,
            status="in_progress",
            claim_token=str(uuid4()),
            claimed_at=_NOW,
            lease_expires_at=_NOW,
        )


def test_dedup_key_active_unique_preserved(conn: sqlite3.Connection) -> None:
    _insert_task(conn, dedup_key="job-42")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(conn, dedup_key="job-42")


def test_p0_p1_columns_all_survive_recreation(conn: sqlite3.Connection) -> None:
    cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(agent_tasks)").fetchall()
    }
    assert {
        "task_id", "trigger_kind", "enqueued_by", "payload_signature",
        "tenant_id", "operator_id", "instruction", "payload_json", "status",
        "dedup_key", "priority", "claim_token", "claimed_at", "lease_expires_at",
        "heartbeat_at", "idempotency_key", "retry_count", "max_retries",
        "next_attempt_at", "last_error", "execution_audit_entry_id",
        "execution_head_hash", "kind", "worker_id", "conversation_id",
        "created_at", "updated_at",
        # P2 nueva
        "trigger_instance_id",
    } <= cols


def test_p0_p1_indexes_all_survive_recreation(conn: sqlite3.Connection) -> None:
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
        "idx_agent_tasks_conversation",
        "idx_agent_tasks_worker_active",
    } <= indexes


# ── Guard de versión 2→3 + idempotencia ─────────────────────────────────────


def test_user_version_advanced_to_p2(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION_P2
    assert _SCHEMA_VERSION_P2 == _SCHEMA_VERSION_P1 + 1


def test_re_running_ensure_is_noop_and_preserves_rows(db_path: Path) -> None:
    c = _connect(db_path)
    ensure_tasks_schema(c)
    instance_id = _authorize_trigger(c, trigger_type="timer")
    auto_id = _insert_task(
        c, trigger_kind="timer", trigger_instance_id=instance_id
    )
    manual_id = _insert_task(c, trigger_kind="manual_enqueue")
    c.close()

    for _ in range(3):
        c = _connect(db_path)
        ensure_tasks_schema(c)
        c.close()

    c = _connect(db_path)
    auto = c.execute(
        "SELECT trigger_kind, trigger_instance_id FROM agent_tasks WHERE task_id=?",
        (auto_id,),
    ).fetchone()
    manual = c.execute(
        "SELECT trigger_kind FROM agent_tasks WHERE task_id=?", (manual_id,)
    ).fetchone()
    # El catálogo no se re-sembró (siguen 3 tipos, sin duplicar).
    type_count = c.execute(
        "SELECT COUNT(*) AS n FROM authorized_trigger_types"
    ).fetchone()["n"]
    # La instancia firmada sobrevive y sigue siendo única.
    inst_count = c.execute(
        "SELECT COUNT(*) AS n FROM authorized_trigger_instances"
    ).fetchone()["n"]
    singletons = c.execute(
        "SELECT COUNT(*) AS n FROM agent_runtime_state"
    ).fetchone()["n"]
    version = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()

    assert auto is not None
    assert auto["trigger_kind"] == "timer"
    assert auto["trigger_instance_id"] == instance_id
    assert manual["trigger_kind"] == "manual_enqueue"
    assert type_count == 3
    assert inst_count == 1
    assert singletons == 1
    assert version == _SCHEMA_VERSION_P2


def test_migration_from_p1_db_advances_to_p2(db_path: Path) -> None:
    # Simula una DB en P1 (user_version=2) con datos, luego corre P2 y verifica
    # que NINGUNA fila se perdió y trigger_instance_id quedó NULL en lo legado.
    from hermes.tasks.infrastructure.schema import (  # noqa: PLC0415
        _DDL_AGENT_RUNTIME_STATE,
        _DDL_AGENT_TASKS_NEW,
        _DDL_AGENT_TASKS_P0,
        _PRAGMAS,
    )

    c = _connect(db_path)
    c.executescript(_PRAGMAS)
    c.executescript(_DDL_AGENT_TASKS_P0)
    c.executescript(_DDL_AGENT_RUNTIME_STATE)
    # Aplica la forma P1 de la tabla (con kind/worker_id/conversation_id) y fija
    # user_version=2 sin la migración P2, replicando una DB ya en P1.
    c.execute("DROP TABLE agent_tasks")
    c.execute(_DDL_AGENT_TASKS_NEW)
    c.execute("ALTER TABLE agent_tasks_new RENAME TO agent_tasks")
    c.execute(f"PRAGMA user_version = {_SCHEMA_VERSION_P1}")
    assert c.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION_P1

    legacy_id = _insert_task(c, instruction="legacy chat", trigger_kind="manual_enqueue")
    c.close()

    c = _connect(db_path)
    ensure_tasks_schema(c)
    row = c.execute(
        "SELECT trigger_kind, trigger_instance_id, kind FROM agent_tasks "
        "WHERE task_id=?",
        (legacy_id,),
    ).fetchone()
    version = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()

    assert row is not None
    assert row["trigger_kind"] == "manual_enqueue"
    assert row["trigger_instance_id"] is None  # legado => sin origen automático
    assert row["kind"] == "autonomous"
    assert version == _SCHEMA_VERSION_P2


def test_idempotent_on_fresh_db_advances_to_p2(db_path: Path) -> None:
    for _ in range(2):
        c = _connect(db_path)
        ensure_tasks_schema(c)
        v = c.execute("PRAGMA user_version").fetchone()[0]
        c.close()
        assert v == _SCHEMA_VERSION_P2
