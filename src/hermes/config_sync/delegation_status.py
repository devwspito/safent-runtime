"""Durable, recipient-bound telemetry. Never grants execution authority.

Observes actual local snapshots; intermediate states not observed are not
invented. Each changed snapshot is committed before HTTP with a stable event ID
and sequence. Concurrent senders may replay the same receipt, never new effects.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

_MAX_SEQUENCE = 2147483647
_COLLECT_LIMIT = 200
_SEND_LIMIT = 8
_TERMINAL = frozenset({"completed", "failed", "rejected", "cancelled", "expired"})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegation_status_state (
 request_id TEXT PRIMARY KEY,
 recipient_instance_id TEXT NOT NULL,
 sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 2147483647),
 status TEXT NOT NULL,
 task_id TEXT
);
CREATE TABLE IF NOT EXISTS delegation_status_outbox (
 event_id TEXT PRIMARY KEY,
 request_id TEXT NOT NULL,
 recipient_instance_id TEXT NOT NULL,
 sequence INTEGER NOT NULL,
 payload TEXT NOT NULL,
 delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1)),
 UNIQUE(request_id,sequence)
);
CREATE INDEX IF NOT EXISTS delegation_status_to_send
 ON delegation_status_outbox(recipient_instance_id,delivered,request_id,sequence);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def collect_status_events(path: Path, *, instance_id: str) -> int:
    """Observe only requests addressed to this exact verified instance."""
    conn = _connect(path)
    try:
        conn.executescript(_SCHEMA)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(pending_delegations)")}
        if "to_instance_id" not in columns:
            return 0  # Not initialized/legacy, not permission to guess provenance.
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """WITH observed AS (
                SELECT d.message_id AS request_id,d.to_instance_id,d.task_id,
                  CASE d.status
                    WHEN 'pending' THEN 'awaiting_approval'
                    WHEN 'rejected' THEN 'rejected'
                    WHEN 'approved' THEN CASE t.status
                        WHEN 'pending' THEN 'queued'
                        WHEN 'in_progress' THEN 'running'
                        WHEN 'pending_approval' THEN 'blocked'
                        WHEN 'completed' THEN 'completed'
                        WHEN 'failed' THEN 'failed'
                        WHEN 'rejected' THEN 'rejected'
                        WHEN 'cancelled' THEN 'cancelled'
                    END
                  END AS observed_status
                FROM pending_delegations d LEFT JOIN agent_tasks t ON t.task_id=d.task_id
                WHERE d.to_instance_id=?
            )
            SELECT o.*,s.sequence AS previous_sequence,s.status AS previous_status,
                s.task_id AS previous_task_id,s.recipient_instance_id AS previous_recipient
            FROM observed o LEFT JOIN delegation_status_state s ON s.request_id=o.request_id
            WHERE o.observed_status IS NOT NULL AND (
                s.request_id IS NULL OR s.status<>o.observed_status
                OR COALESCE(s.task_id,'')<>COALESCE(o.task_id,'')
            ) ORDER BY o.request_id LIMIT ?""",
            (instance_id, _COLLECT_LIMIT),
        ).fetchall()
        for row in rows:
            if row["previous_recipient"] and row["previous_recipient"] != instance_id:
                raise ValueError("delegation recipient changed")
            if row["previous_task_id"] and row["previous_task_id"] != row["task_id"]:
                raise ValueError("delegation execution identity changed")
            if (
                row["previous_status"] in _TERMINAL
                and row["previous_status"] != row["observed_status"]
            ):
                raise ValueError("terminal delegation regressed")
            sequence = (row["previous_sequence"] or 0) + 1
            if sequence > _MAX_SEQUENCE:
                raise ValueError("delegation sequence exhausted")
            event_id = str(uuid4())
            event = {
                "event_id": event_id,
                "sequence": sequence,
                "status": row["observed_status"],
                "task_id": row["task_id"],
            }
            payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
            conn.execute(
                "INSERT INTO delegation_status_outbox"
                "(event_id,request_id,recipient_instance_id,sequence,payload) "
                "VALUES(?,?,?,?,?)",
                (event_id, row["request_id"], instance_id, sequence, payload),
            )
            conn.execute(
                "INSERT INTO delegation_status_state"
                "(request_id,recipient_instance_id,sequence,status,task_id) "
                "VALUES(?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET "
                "sequence=excluded.sequence,status=excluded.status,task_id=excluded.task_id",
                (row["request_id"], instance_id, sequence, row["observed_status"], row["task_id"]),
            )
        conn.execute("COMMIT")
        return len(rows)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def push_status_events(
    path: Path,
    *,
    instance_id: str,
    cloud_endpoint: str,
    instance_secret: str,
    is_current: Callable[[], bool],
) -> int:
    """Bounded flush; exact receipt required. Failure retains the original event.

    The caller has validated endpoint/pairing. Re-check pairing before EVERY
    send so revocation/reassociation does not drain an old outbox to a new org.
    Never logs a payload, bearer, response body or arbitrary provider error.
    """
    collect_status_events(path, instance_id=instance_id)
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM delegation_status_outbox WHERE recipient_instance_id=? AND delivered=0 "
            "ORDER BY request_id,sequence LIMIT ?",
            (instance_id, _SEND_LIMIT),
        ).fetchall()
        delivered = 0
        for row in rows:
            if not is_current():
                break
            try:
                response = httpx.post(
                    f"{cloud_endpoint.rstrip('/')}/v1/delegations/"
                    f"{quote(row['request_id'], safe='')}/status",
                    headers={"Authorization": f"Bearer {instance_secret}"},
                    json=json.loads(row["payload"]),
                    timeout=2.0,
                    follow_redirects=False,
                )
                if response.status_code != httpx.codes.OK:
                    break
                receipt = response.json()
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("accepted") is not True
                    or receipt.get("event_id") != row["event_id"]
                    or type(receipt.get("sequence")) is not int
                    or receipt["sequence"] != row["sequence"]
                    or type(receipt.get("ignored")) is not bool
                ):
                    break
            except (httpx.HTTPError, ValueError):
                break
            conn.execute(
                "UPDATE delegation_status_outbox SET delivered=1 WHERE event_id=? AND payload=?",
                (row["event_id"], row["payload"]),
            )
            delivered += 1
        return delivered
    finally:
        conn.close()
