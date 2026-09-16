"""Cancellation must persist as terminal, without weakening execution evidence."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from hermes.tasks.domain.ports import TaskStatus, WorkItem
from hermes.tasks.infrastructure import schema
from hermes.tasks.infrastructure.sqlite_work_queue import ClaimTokenMismatch, SqliteWorkQueue
from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue

pytestmark = pytest.mark.unit


def item(dedup_key=None):
    return WorkItem.new(
        tenant_id=uuid4(),
        trigger_kind="manual_enqueue",
        payload={"instruction": "cancel test", "enqueued_by": "owner"},
        dedup_key=dedup_key,
    )


async def test_memory_queue_matches_sqlite_terminal_dedup():
    queue = InMemoryWorkQueue()
    original = await queue.enqueue(item("unique-job"))
    claimed = await queue.claim_next()
    await queue.mark_cancelled(claimed.id, claim_token=claimed.claim_token, reason="owner")
    assert await queue.find_by_dedup_key("unique-job") is None
    assert (await queue.enqueue(item("unique-job"))).id != original.id


async def test_real_queue_cancel_survives_restart_and_allows_new_dedup(tmp_path):
    path = tmp_path / "queue.db"
    queue = SqliteWorkQueue(db_path=path)
    original = await queue.enqueue(item("unique-job"))
    claimed = await queue.claim_next()
    assert claimed.id == original.id
    await queue.mark_cancelled(claimed.id, claim_token=claimed.claim_token, reason="owner stopped")
    restarted = SqliteWorkQueue(db_path=path)
    loaded = await restarted._load_item(str(original.id))
    assert loaded.status is TaskStatus.CANCELLED
    assert loaded.claim_token is None and loaded.lease_expires_at is None
    assert await restarted.reconcile_stale() == 0
    assert await restarted.claim_next() is None
    second = await restarted.enqueue(item("unique-job"))
    assert second.id != original.id


async def test_wrong_claim_never_cancels_and_terminal_cannot_be_cancelled_twice(tmp_path):
    queue = SqliteWorkQueue(db_path=tmp_path / "queue.db")
    await queue.enqueue(item())
    claimed = await queue.claim_next()
    with pytest.raises(ClaimTokenMismatch):
        await queue.mark_cancelled(claimed.id, claim_token=uuid4(), reason="wrong")
    assert (await queue._load_item(str(claimed.id))).status is TaskStatus.IN_PROGRESS
    await queue.mark_cancelled(claimed.id, claim_token=claimed.claim_token, reason="owner")
    with pytest.raises(ClaimTokenMismatch):
        await queue.mark_cancelled(claimed.id, claim_token=claimed.claim_token, reason="again")


async def test_cancellation_check_requires_claim_cleared_and_completion_still_needs_evidence(
    tmp_path,
):
    path = tmp_path / "queue.db"
    queue = SqliteWorkQueue(db_path=path)
    await queue.enqueue(item())
    claimed = await queue.claim_next()
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE agent_tasks SET status='cancelled' WHERE task_id=?", (str(claimed.id),)
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE agent_tasks SET status='completed',claim_token=NULL,lease_expires_at=NULL "
                "WHERE task_id=?",
                (str(claimed.id),),
            )


def legacy_database(path):
    conn = sqlite3.connect(path, isolation_level=None)
    schema.ensure_tasks_schema(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(schema._DDL_AGENT_TASKS_NEW_P4)
    conn.execute("DROP TABLE agent_tasks")
    conn.execute("ALTER TABLE agent_tasks_new RENAME TO agent_tasks")
    conn.executescript(schema._DDL_AGENT_TASKS_INDEXES)
    conn.execute("PRAGMA user_version=5")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""INSERT INTO agent_tasks(task_id,trigger_kind,enqueued_by,operator_id,
        instruction,status,created_at,updated_at,payload_signature,execution_audit_entry_id,
        execution_head_hash) VALUES ('legacy','manual_enqueue','owner','owner','unchanged',
        'completed','2026-09-11','2026-09-11','signed','audit','hash')""")
    return conn


def test_upgrade_preserves_rows_inbound_fk_custom_indexes_and_triggers(tmp_path):
    conn = legacy_database(tmp_path / "legacy.db")
    conn.executescript("""
        CREATE TABLE dependent(task_id TEXT REFERENCES agent_tasks(task_id));
        INSERT INTO dependent VALUES ('legacy');
        CREATE TABLE change_log(task_id TEXT);
        CREATE INDEX custom_task_index ON agent_tasks(updated_at);
        CREATE TRIGGER custom_task_trigger AFTER UPDATE ON agent_tasks
        BEGIN INSERT INTO change_log VALUES(NEW.task_id); END;
    """)
    before = conn.execute("SELECT * FROM agent_tasks").fetchall()
    schema.ensure_tasks_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == schema._SCHEMA_VERSION_P5
    assert conn.execute("SELECT * FROM agent_tasks").fetchall() == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert conn.execute("SELECT * FROM dependent").fetchall() == [("legacy",)]
    objects = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE tbl_name='agent_tasks'")
    }
    assert {
        "custom_task_index",
        "custom_task_trigger",
        "agent_tasks_dedup_key_active_unique",
    } <= objects
    conn.execute("UPDATE agent_tasks SET updated_at='later' WHERE task_id='legacy'")
    assert conn.execute("SELECT * FROM change_log").fetchall() == [("legacy",)]
    schema.ensure_tasks_schema(conn)
    assert conn.execute("SELECT * FROM change_log").fetchall() == [("legacy",)]
    conn.close()


def test_unknown_columns_fail_without_losing_data(tmp_path):
    conn = legacy_database(tmp_path / "unknown.db")
    conn.execute("ALTER TABLE agent_tasks ADD COLUMN future_data TEXT DEFAULT 'preserve'")
    before = conn.execute("SELECT * FROM agent_tasks").fetchall()
    with pytest.raises(sqlite3.OperationalError, match="unrecognized"):
        schema.ensure_tasks_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone() == (5,)
    assert conn.execute("SELECT * FROM agent_tasks").fetchall() == before
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    conn.close()


def test_failed_foreign_key_validation_rolls_back_schema_and_rows(tmp_path):
    conn = legacy_database(tmp_path / "broken-fk.db")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("CREATE TABLE dependent(task_id TEXT REFERENCES agent_tasks(task_id))")
    conn.execute("INSERT INTO dependent VALUES ('missing')")
    conn.execute("PRAGMA foreign_keys=ON")
    before = conn.execute("SELECT sql FROM sqlite_master WHERE name='agent_tasks'").fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="foreign-key"):
        schema.ensure_tasks_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone() == (5,)
    assert (
        conn.execute("SELECT sql FROM sqlite_master WHERE name='agent_tasks'").fetchone() == before
    )
    assert conn.execute("SELECT task_id FROM agent_tasks").fetchall() == [("legacy",)]
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    conn.close()


def test_concurrent_upgrade_rechecks_version_after_lock(tmp_path):
    path = tmp_path / "race.db"
    legacy_database(path).close()

    def migrate(_):
        conn = sqlite3.connect(path, isolation_level=None, timeout=10)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            schema._recreate_p5_cancellation_if_needed(conn)
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(migrate, range(8))) == [schema._SCHEMA_VERSION_P5] * 8
