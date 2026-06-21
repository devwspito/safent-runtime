"""SQLite-backed ScanRecord repository — WAL mode, /var/lib/hermes/security/scans.db."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.policy import SecurityPolicy
from hermes.security_center.domain.scan_record import ScanDecision, ScanRecord
from hermes.security_center.domain.scan_score import InstallScore, Risk, Severity, Verdict

logger = logging.getLogger("hermes.security_center.sqlite_scan_repo")

_DB_PATH = Path("/var/lib/hermes/security/scans.db")

_DDL = """
CREATE TABLE IF NOT EXISTS scan_records (
    scan_id         TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    identifier      TEXT NOT NULL,
    source_url      TEXT NOT NULL DEFAULT '',
    version         TEXT NOT NULL DEFAULT '',
    sha256          TEXT NOT NULL DEFAULT '',
    cache_key       TEXT NOT NULL,
    manifest_json   TEXT NOT NULL DEFAULT '',
    score           INTEGER NOT NULL,
    verdict         TEXT NOT NULL,
    decision        TEXT NOT NULL DEFAULT 'PENDING',
    risks_json      TEXT NOT NULL DEFAULT '[]',
    cached          INTEGER NOT NULL DEFAULT 0,
    elapsed_ms      INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scan_records_cache_key
    ON scan_records (cache_key, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_scan_records_finished_at
    ON scan_records (finished_at DESC);
"""

_POLICY_DDL = """
CREATE TABLE IF NOT EXISTS security_policy (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    policy_json     TEXT NOT NULL
);
"""


class SQLiteScanRepo:
    """Thread-safe per-call connection pattern (mirrors SQLiteConsentRepository).

    WAL journal mode enabled at schema init for concurrent read performance.
    """

    def __init__(self, *, db_path: Path | None = None) -> None:
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_DDL)

    def save(self, record: ScanRecord) -> None:
        risks_json = json.dumps([
            {
                "category": r.category,
                "severity": r.severity.value,
                "message": r.message,
                "evidence_ref": r.evidence_ref,
            }
            for r in record.score.risks
        ])
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO scan_records
                    (scan_id, kind, identifier, source_url, version, sha256,
                     cache_key, manifest_json, score, verdict, decision, risks_json,
                     cached, elapsed_ms, started_at, finished_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(record.id),
                    record.target.kind,
                    record.target.identifier,
                    record.target.source_url,
                    record.target.version,
                    record.target.sha256,
                    record.target.cache_key,
                    record.target.manifest_json,
                    record.score.value,
                    record.verdict.value,
                    record.decision,
                    risks_json,
                    int(record.cached),
                    record.elapsed_ms,
                    record.started_at.isoformat(),
                    record.finished_at.isoformat(),
                ),
            )

    def get(self, scan_id: UUID) -> ScanRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scan_records WHERE scan_id = ?", (str(scan_id),)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def get_by_cache_key(self, cache_key: str) -> ScanRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM scan_records
                   WHERE cache_key = ?
                   ORDER BY finished_at DESC LIMIT 1""",
                (cache_key,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_recent(self, *, limit: int) -> list[ScanRecord]:
        safe_limit = min(max(int(limit), 1), 200)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scan_records ORDER BY finished_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def update_decision(self, scan_id: UUID, decision: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE scan_records SET decision = ? WHERE scan_id = ?",
                (decision, str(scan_id)),
            )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ScanRecord:
        risks_raw = json.loads(row["risks_json"] or "[]")
        risks = tuple(
            Risk(
                category=r["category"],
                severity=Severity(r["severity"]),
                message=r["message"],
                evidence_ref=r.get("evidence_ref", ""),
            )
            for r in risks_raw
        )
        target = InstallTarget(
            kind=row["kind"],
            identifier=row["identifier"],
            source_url=row["source_url"] or "",
            version=row["version"] or "",
            sha256=row["sha256"] or "",
            manifest_json=row["manifest_json"] or "",
        )
        return ScanRecord(
            id=UUID(row["scan_id"]),
            target=target,
            score=InstallScore(value=int(row["score"]), risks=risks),
            verdict=Verdict(row["verdict"]),
            decision=row["decision"],
            started_at=datetime.fromisoformat(row["started_at"]).replace(tzinfo=UTC),
            finished_at=datetime.fromisoformat(row["finished_at"]).replace(tzinfo=UTC),
            cached=bool(row["cached"]),
            elapsed_ms=int(row["elapsed_ms"]),
        )


class SQLitePolicyRepo:
    """Stores a single SecurityPolicy row (id=1) in the same DB."""

    def __init__(self, *, db_path: Path | None = None) -> None:
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_POLICY_DDL)

    def load(self) -> SecurityPolicy:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT policy_json FROM security_policy WHERE id = 1"
            ).fetchone()
        if row is None:
            return SecurityPolicy.default()
        try:
            raw = json.loads(row["policy_json"])
            return SecurityPolicy(
                auto_block_fail=bool(raw.get("auto_block_fail", True)),
                require_approval_warn=bool(raw.get("require_approval_warn", True)),
                scanner_weights=dict(raw.get("scanner_weights", {"cve": 35, "mcp_lint": 30, "provenance": 20, "signature": 15})),
                trusted_orgs=frozenset(raw.get("trusted_orgs", [])),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.security.policy_load_failed: %s — using default", exc)
            return SecurityPolicy.default()

    def save(self, policy: SecurityPolicy) -> None:
        payload = json.dumps(policy.to_dict())
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO security_policy (id, policy_json) VALUES (1, ?)",
                (payload,),
            )
