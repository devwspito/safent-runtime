"""SqlitePlatformModelRegistry — daemon-owned persistence (T016/T018).

Follows EXACTLY the pattern of SqliteAgentRegistry:
- WAL mode + isolation_level=None (autocommit).
- Single-writer (the daemon). Shell-server only reads via D-Bus.
- INSERT OR IGNORE for idempotent seed ops (race-safe).
- Row factory for named column access.

Schema is additive (expand-only): no ALTER on existing tables.
PII is NEVER persisted — only domain names, references, and hashes (SC-008).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from hermes.platforms.domain.model_gap import (
    DirectedTeachingRequest,
    GapState,
    ModelGap,
    TeachingRequestState,
)
from hermes.platforms.domain.platform_learning_tour import (
    PlatformLearningTour,
    TourOrigin,
    TourScope,
    TourState,
)
from hermes.platforms.domain.platform_model import (
    BusinessEntity,
    HouseRule,
    HouseRuleKind,
    NavigationLandmark,
    PlatformArea,
    PlatformModel,
    StalenessMark,
    Zone,
)
from hermes.platforms.domain.ports import (
    ModelGapNotFound,
    PlatformModelNotFound,
    PlatformTourNotFound,
)
from hermes.platforms.domain.value_objects import (
    ActionRef,
    DomainName,
    EntityRelationship,
    LandmarkKind,
    LifecycleState,
    ModelVersion,
    NavigationPath,
    PlatformModelId,
    PlatformModelSignature,
    TeachingModality,
    TourOrigin as TourOriginVO,
    ZoneHash,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_models (
  model_id          TEXT NOT NULL,
  version           INTEGER NOT NULL,
  tenant_id         TEXT NOT NULL,
  site_ref          TEXT NOT NULL,
  lifecycle_state   TEXT NOT NULL,
  origin            TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  PRIMARY KEY (model_id, tenant_id, version)
);
CREATE INDEX IF NOT EXISTS idx_pm_tenant ON platform_models (tenant_id);
CREATE INDEX IF NOT EXISTS idx_pm_state ON platform_models (lifecycle_state);

CREATE TABLE IF NOT EXISTS platform_zones (
  zone_id           TEXT NOT NULL,
  model_id          TEXT NOT NULL,
  tenant_id         TEXT NOT NULL,
  model_version     INTEGER NOT NULL,
  zone_hash         TEXT NOT NULL,
  member_refs       TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (zone_id, model_id, tenant_id, model_version)
);
CREATE INDEX IF NOT EXISTS idx_pz_model ON platform_zones (model_id, tenant_id, model_version);

CREATE TABLE IF NOT EXISTS platform_areas (
  area_id           TEXT NOT NULL,
  model_id          TEXT NOT NULL,
  tenant_id         TEXT NOT NULL,
  model_version     INTEGER NOT NULL,
  domain_name       TEXT,
  needs_label       INTEGER NOT NULL DEFAULT 0,
  navigation_path   TEXT NOT NULL,
  zone_id           TEXT NOT NULL,
  available_actions TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (area_id, model_id, tenant_id, model_version)
);
CREATE INDEX IF NOT EXISTS idx_pa_model ON platform_areas (model_id, tenant_id, model_version);

CREATE TABLE IF NOT EXISTS business_entities (
  entity_id         TEXT NOT NULL,
  model_id          TEXT NOT NULL,
  tenant_id         TEXT NOT NULL,
  model_version     INTEGER NOT NULL,
  domain_name       TEXT NOT NULL,
  relationships     TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (entity_id, model_id, tenant_id, model_version)
);

CREATE TABLE IF NOT EXISTS navigation_landmarks (
  landmark_id       TEXT NOT NULL,
  model_id          TEXT NOT NULL,
  tenant_id         TEXT NOT NULL,
  model_version     INTEGER NOT NULL,
  kind              TEXT NOT NULL,
  locator_ref       TEXT NOT NULL,
  zone_id           TEXT NOT NULL,
  is_stale          INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (landmark_id, model_id, tenant_id, model_version)
);

CREATE TABLE IF NOT EXISTS house_rules (
  rule_id           TEXT NOT NULL,
  model_id          TEXT NOT NULL,
  tenant_id         TEXT NOT NULL,
  model_version     INTEGER NOT NULL,
  kind              TEXT NOT NULL,
  target_area_ref   TEXT NOT NULL,
  phrasing          TEXT NOT NULL,
  PRIMARY KEY (rule_id, model_id, tenant_id, model_version)
);

CREATE TABLE IF NOT EXISTS staleness_marks (
  zone_id             TEXT NOT NULL,
  model_id            TEXT NOT NULL,
  tenant_id           TEXT NOT NULL,
  model_version       INTEGER NOT NULL,
  detected_at         TEXT NOT NULL,
  reason              TEXT NOT NULL,
  relearn_request_id  TEXT NOT NULL,
  PRIMARY KEY (zone_id, model_id, tenant_id, model_version)
);

CREATE TABLE IF NOT EXISTS platform_model_signatures (
  model_id            TEXT NOT NULL,
  tenant_id           TEXT NOT NULL,
  version             INTEGER NOT NULL,
  origin_attribution  TEXT NOT NULL,
  content_hash        TEXT NOT NULL,
  per_zone_hashes     TEXT NOT NULL DEFAULT '[]',
  signature_hex       TEXT NOT NULL,
  PRIMARY KEY (model_id, tenant_id, version)
);

CREATE TABLE IF NOT EXISTS platform_learning_tours (
  tour_id               TEXT PRIMARY KEY,
  tenant_id             TEXT NOT NULL,
  target_site_ref       TEXT NOT NULL,
  origin                TEXT NOT NULL,
  modality              TEXT NOT NULL,
  scope                 TEXT NOT NULL DEFAULT 'full',
  operator_attribution  INTEGER,
  state                 TEXT NOT NULL DEFAULT 'open',
  captured_areas        TEXT NOT NULL DEFAULT '[]',
  narration_transcript_ref TEXT,
  opened_at             TEXT NOT NULL,
  closed_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_plt_tenant ON platform_learning_tours (tenant_id);

CREATE TABLE IF NOT EXISTS model_gaps (
  gap_id              TEXT PRIMARY KEY,
  platform_model_id   TEXT NOT NULL,
  task_ref            TEXT NOT NULL,
  missing_descriptor  TEXT NOT NULL,
  context             TEXT NOT NULL DEFAULT '',
  teaching_request_id TEXT NOT NULL,
  state               TEXT NOT NULL DEFAULT 'open',
  detected_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mg_model ON model_gaps (platform_model_id);

CREATE TABLE IF NOT EXISTS directed_teaching_requests (
  request_id                TEXT PRIMARY KEY,
  platform_model_id         TEXT NOT NULL,
  reason                    TEXT NOT NULL,
  target_zone_or_descriptor TEXT NOT NULL,
  state                     TEXT NOT NULL DEFAULT 'open'
);
"""


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class SqlitePlatformModelRegistry:
    """SQLite WAL persistence for PlatformModel and related aggregates.

    Follows the exact pattern of SqliteAgentRegistry: single-writer daemon,
    WAL autocommit, INSERT OR IGNORE for idempotency.
    """

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # PlatformModel persistence
    # ------------------------------------------------------------------

    def save(self, model: PlatformModel) -> None:
        """Upsert a PlatformModel and all its child tables atomically."""
        with self._connect() as conn:
            # Models uses INSERT OR REPLACE to handle version bumps.
            conn.execute(
                """
                INSERT OR REPLACE INTO platform_models
                  (model_id, tenant_id, version, site_ref, lifecycle_state, origin,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(model.platform_model_id),
                    model.tenant_id,
                    model.version.number,
                    model.site_ref,
                    str(model.lifecycle_state),
                    str(model.origin),
                    model.created_at.isoformat(),
                    model.updated_at.isoformat(),
                ),
            )
            self._save_zones(conn, model)
            self._save_areas(conn, model)
            self._save_entities(conn, model)
            self._save_landmarks(conn, model)
            self._save_house_rules(conn, model)
            self._save_staleness_marks(conn, model)
            if model.signature is not None:
                self._save_signature(conn, model)

    def _save_zones(self, conn: sqlite3.Connection, model: PlatformModel) -> None:
        model_id = str(model.platform_model_id)
        tid = model.tenant_id
        version = model.version.number
        conn.execute(
            "DELETE FROM platform_zones WHERE model_id=? AND tenant_id=? AND model_version=?",
            (model_id, tid, version),
        )
        for z in model.zones:
            conn.execute(
                """
                INSERT OR IGNORE INTO platform_zones
                  (zone_id, model_id, tenant_id, model_version, zone_hash, member_refs)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (z.zone_id, model_id, tid, version,
                 z.zone_hash.hex_digest,
                 json.dumps(list(z.member_refs))),
            )

    def _save_areas(self, conn: sqlite3.Connection, model: PlatformModel) -> None:
        model_id = str(model.platform_model_id)
        tid = model.tenant_id
        version = model.version.number
        conn.execute(
            "DELETE FROM platform_areas WHERE model_id=? AND tenant_id=? AND model_version=?",
            (model_id, tid, version),
        )
        for a in model.areas:
            conn.execute(
                """
                INSERT OR IGNORE INTO platform_areas
                  (area_id, model_id, tenant_id, model_version, domain_name, needs_label,
                   navigation_path, zone_id, available_actions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    a.area_id, model_id, tid, version,
                    str(a.domain_name) if a.domain_name else None,
                    1 if a.needs_label else 0,
                    str(a.navigation_path),
                    a.zone_id,
                    json.dumps([ac.name for ac in a.available_actions]),
                ),
            )

    def _save_entities(self, conn: sqlite3.Connection, model: PlatformModel) -> None:
        model_id = str(model.platform_model_id)
        tid = model.tenant_id
        version = model.version.number
        conn.execute(
            "DELETE FROM business_entities WHERE model_id=? AND tenant_id=? AND model_version=?",
            (model_id, tid, version),
        )
        for e in model.entities:
            rels = [
                {"target_entity_id": r.target_entity_id, "description": r.description}
                for r in e.relationships
            ]
            conn.execute(
                """
                INSERT OR IGNORE INTO business_entities
                  (entity_id, model_id, tenant_id, model_version, domain_name, relationships)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (e.entity_id, model_id, tid, version, str(e.domain_name), json.dumps(rels)),
            )

    def _save_landmarks(self, conn: sqlite3.Connection, model: PlatformModel) -> None:
        model_id = str(model.platform_model_id)
        tid = model.tenant_id
        version = model.version.number
        conn.execute(
            "DELETE FROM navigation_landmarks WHERE model_id=? AND tenant_id=? AND model_version=?",
            (model_id, tid, version),
        )
        for lm in model.landmarks:
            conn.execute(
                """
                INSERT OR IGNORE INTO navigation_landmarks
                  (landmark_id, model_id, tenant_id, model_version, kind, locator_ref,
                   zone_id, is_stale)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lm.landmark_id, model_id, tid, version,
                    str(lm.kind), lm.locator_ref, lm.zone_id,
                    1 if lm.is_stale else 0,
                ),
            )

    def _save_house_rules(self, conn: sqlite3.Connection, model: PlatformModel) -> None:
        model_id = str(model.platform_model_id)
        tid = model.tenant_id
        version = model.version.number
        conn.execute(
            "DELETE FROM house_rules WHERE model_id=? AND tenant_id=? AND model_version=?",
            (model_id, tid, version),
        )
        for r in model.house_rules:
            conn.execute(
                """
                INSERT OR IGNORE INTO house_rules
                  (rule_id, model_id, tenant_id, model_version, kind, target_area_ref, phrasing)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (r.rule_id, model_id, tid, version, str(r.kind), r.target_area_ref, r.phrasing),
            )

    def _save_staleness_marks(self, conn: sqlite3.Connection, model: PlatformModel) -> None:
        model_id = str(model.platform_model_id)
        tid = model.tenant_id
        version = model.version.number
        conn.execute(
            "DELETE FROM staleness_marks WHERE model_id=? AND tenant_id=? AND model_version=?",
            (model_id, tid, version),
        )
        for sm in model.staleness_marks:
            conn.execute(
                """
                INSERT OR IGNORE INTO staleness_marks
                  (zone_id, model_id, tenant_id, model_version, detected_at, reason,
                   relearn_request_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sm.zone_id, model_id, tid, version,
                    sm.detected_at.isoformat(),
                    sm.reason, sm.relearn_request_id,
                ),
            )

    def _save_signature(self, conn: sqlite3.Connection, model: PlatformModel) -> None:
        sig = model.signature
        conn.execute(
            """
            INSERT OR REPLACE INTO platform_model_signatures
              (model_id, tenant_id, version, origin_attribution, content_hash,
               per_zone_hashes, signature_hex)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(model.platform_model_id),
                model.tenant_id,
                model.version.number,
                sig.origin_attribution,
                sig.content_hash,
                json.dumps(list(sig.per_zone_hashes)),
                sig.signature_hex,
            ),
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, model_id: str, tenant_id: str) -> PlatformModel:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM platform_models
                WHERE model_id=? AND tenant_id=?
                ORDER BY version DESC LIMIT 1
                """,
                (model_id, tenant_id),
            ).fetchone()
        if row is None:
            raise PlatformModelNotFound(model_id)
        return self._row_to_model(row)

    def list_by_tenant(self, tenant_id: str) -> list[PlatformModel]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM platform_models WHERE tenant_id=?
                ORDER BY updated_at DESC
                """,
                (tenant_id,),
            ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def _row_to_model(self, row: sqlite3.Row) -> PlatformModel:
        model_id = row["model_id"]
        tenant_id = row["tenant_id"]
        version = row["version"]
        with self._connect() as conn:
            areas = self._load_areas(conn, model_id, tenant_id, version)
            entities = self._load_entities(conn, model_id, tenant_id, version)
            landmarks = self._load_landmarks(conn, model_id, tenant_id, version)
            house_rules = self._load_house_rules(conn, model_id, tenant_id, version)
            zones = self._load_zones(conn, model_id, tenant_id, version)
            staleness_marks = self._load_staleness_marks(conn, model_id, tenant_id, version)
            signature = self._load_signature(conn, model_id, tenant_id, version)
        return PlatformModel(
            platform_model_id=PlatformModelId(model_id),
            version=ModelVersion(version),
            tenant_id=row["tenant_id"],
            site_ref=row["site_ref"],
            lifecycle_state=LifecycleState(row["lifecycle_state"]),
            origin=TourOrigin(row["origin"]),
            areas=tuple(areas),
            entities=tuple(entities),
            landmarks=tuple(landmarks),
            house_rules=tuple(house_rules),
            zones=tuple(zones),
            staleness_marks=tuple(staleness_marks),
            signature=signature,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _load_zones(
        self, conn: sqlite3.Connection, model_id: str, tenant_id: str, version: int
    ) -> list[Zone]:
        rows = conn.execute(
            "SELECT * FROM platform_zones WHERE model_id=? AND tenant_id=? AND model_version=?",
            (model_id, tenant_id, version),
        ).fetchall()
        return [
            Zone(
                zone_id=r["zone_id"],
                zone_hash=ZoneHash(r["zone_hash"]),
                member_refs=tuple(json.loads(r["member_refs"])),
            )
            for r in rows
        ]

    def _load_areas(
        self, conn: sqlite3.Connection, model_id: str, tenant_id: str, version: int
    ) -> list[PlatformArea]:
        rows = conn.execute(
            "SELECT * FROM platform_areas WHERE model_id=? AND tenant_id=? AND model_version=?",
            (model_id, tenant_id, version),
        ).fetchall()
        result = []
        for r in rows:
            actions = tuple(ActionRef(name=n) for n in json.loads(r["available_actions"]))
            result.append(
                PlatformArea(
                    area_id=r["area_id"],
                    domain_name=DomainName(r["domain_name"]) if r["domain_name"] else None,
                    needs_label=bool(r["needs_label"]),
                    navigation_path=NavigationPath(r["navigation_path"]),
                    zone_id=r["zone_id"],
                    available_actions=actions,
                )
            )
        return result

    def _load_entities(
        self, conn: sqlite3.Connection, model_id: str, tenant_id: str, version: int
    ) -> list[BusinessEntity]:
        rows = conn.execute(
            "SELECT * FROM business_entities WHERE model_id=? AND tenant_id=? AND model_version=?",
            (model_id, tenant_id, version),
        ).fetchall()
        result = []
        for r in rows:
            rels_raw = json.loads(r["relationships"])
            rels = tuple(
                EntityRelationship(
                    target_entity_id=rel["target_entity_id"],
                    description=rel["description"],
                )
                for rel in rels_raw
            )
            result.append(
                BusinessEntity(
                    entity_id=r["entity_id"],
                    domain_name=DomainName(r["domain_name"]),
                    relationships=rels,
                )
            )
        return result

    def _load_landmarks(
        self, conn: sqlite3.Connection, model_id: str, tenant_id: str, version: int
    ) -> list[NavigationLandmark]:
        rows = conn.execute(
            "SELECT * FROM navigation_landmarks WHERE model_id=? AND tenant_id=? AND model_version=?",
            (model_id, tenant_id, version),
        ).fetchall()
        return [
            NavigationLandmark(
                landmark_id=r["landmark_id"],
                kind=LandmarkKind(r["kind"]),
                locator_ref=r["locator_ref"],
                zone_id=r["zone_id"],
                is_stale=bool(r["is_stale"]),
            )
            for r in rows
        ]

    def _load_house_rules(
        self, conn: sqlite3.Connection, model_id: str, tenant_id: str, version: int
    ) -> list[HouseRule]:
        rows = conn.execute(
            "SELECT * FROM house_rules WHERE model_id=? AND tenant_id=? AND model_version=?",
            (model_id, tenant_id, version),
        ).fetchall()
        return [
            HouseRule(
                rule_id=r["rule_id"],
                kind=HouseRuleKind(r["kind"]),
                target_area_ref=r["target_area_ref"],
                phrasing=r["phrasing"],
            )
            for r in rows
        ]

    def _load_staleness_marks(
        self, conn: sqlite3.Connection, model_id: str, tenant_id: str, version: int
    ) -> list[StalenessMark]:
        rows = conn.execute(
            "SELECT * FROM staleness_marks WHERE model_id=? AND tenant_id=? AND model_version=?",
            (model_id, tenant_id, version),
        ).fetchall()
        return [
            StalenessMark(
                zone_id=r["zone_id"],
                detected_at=datetime.fromisoformat(r["detected_at"]),
                reason=r["reason"],
                relearn_request_id=r["relearn_request_id"],
            )
            for r in rows
        ]

    def _load_signature(
        self, conn: sqlite3.Connection, model_id: str, tenant_id: str, version: int
    ) -> PlatformModelSignature | None:
        row = conn.execute(
            "SELECT * FROM platform_model_signatures WHERE model_id=? AND tenant_id=? AND version=?",
            (model_id, tenant_id, version),
        ).fetchone()
        if row is None:
            return None
        return PlatformModelSignature(
            platform_model_id=row["model_id"],
            version=row["version"],
            tenant_id=row["tenant_id"],
            origin_attribution=row["origin_attribution"],
            content_hash=row["content_hash"],
            per_zone_hashes=tuple(json.loads(row["per_zone_hashes"])),
            signature_hex=row["signature_hex"],
        )

    # ------------------------------------------------------------------
    # PlatformLearningTour persistence
    # ------------------------------------------------------------------

    def save_tour(self, tour: PlatformLearningTour) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO platform_learning_tours
                  (tour_id, tenant_id, target_site_ref, origin, modality, scope,
                   operator_attribution, state, captured_areas, narration_transcript_ref,
                   opened_at, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tour.tour_id,
                    tour.tenant_id,
                    tour.target_site_ref,
                    str(tour.origin),
                    str(tour.modality),
                    str(tour.scope),
                    tour.operator_attribution,
                    str(tour.state),
                    json.dumps(list(tour.captured_areas)),
                    tour.narration_transcript_ref,
                    tour.opened_at.isoformat(),
                    tour.closed_at.isoformat() if tour.closed_at else None,
                ),
            )

    def get_tour(self, tour_id: str) -> PlatformLearningTour:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_learning_tours WHERE tour_id=?",
                (tour_id,),
            ).fetchone()
        if row is None:
            raise PlatformTourNotFound(tour_id)
        return PlatformLearningTour(
            tour_id=row["tour_id"],
            tenant_id=row["tenant_id"],
            target_site_ref=row["target_site_ref"],
            origin=TourOriginVO(row["origin"]),
            modality=TeachingModality(row["modality"]),
            scope=TourScope(row["scope"]),
            operator_attribution=row["operator_attribution"],
            state=TourState(row["state"]),
            captured_areas=tuple(json.loads(row["captured_areas"])),
            narration_transcript_ref=row["narration_transcript_ref"],
            opened_at=datetime.fromisoformat(row["opened_at"]),
            closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
        )

    # ------------------------------------------------------------------
    # ModelGap persistence
    # ------------------------------------------------------------------

    def save_gap(self, gap: ModelGap) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO model_gaps
                  (gap_id, platform_model_id, task_ref, missing_descriptor, context,
                   teaching_request_id, state, detected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gap.gap_id,
                    gap.platform_model_id,
                    gap.task_ref,
                    gap.missing_descriptor,
                    gap.context,
                    gap.teaching_request_id,
                    str(gap.state),
                    gap.detected_at.isoformat(),
                ),
            )

    def get_gap(self, gap_id: str) -> ModelGap:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM model_gaps WHERE gap_id=?", (gap_id,)
            ).fetchone()
        if row is None:
            raise ModelGapNotFound(gap_id)
        return ModelGap(
            gap_id=row["gap_id"],
            platform_model_id=row["platform_model_id"],
            task_ref=row["task_ref"],
            missing_descriptor=row["missing_descriptor"],
            context=row["context"],
            teaching_request_id=row["teaching_request_id"],
            state=GapState(row["state"]),
            detected_at=datetime.fromisoformat(row["detected_at"]),
        )

    def list_gaps(self, model_id: str) -> list[ModelGap]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM model_gaps WHERE platform_model_id=? ORDER BY detected_at DESC",
                (model_id,),
            ).fetchall()
        return [
            ModelGap(
                gap_id=r["gap_id"],
                platform_model_id=r["platform_model_id"],
                task_ref=r["task_ref"],
                missing_descriptor=r["missing_descriptor"],
                context=r["context"],
                teaching_request_id=r["teaching_request_id"],
                state=GapState(r["state"]),
                detected_at=datetime.fromisoformat(r["detected_at"]),
            )
            for r in rows
        ]

    def save_teaching_request(self, request: DirectedTeachingRequest) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO directed_teaching_requests
                  (request_id, platform_model_id, reason, target_zone_or_descriptor, state)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.platform_model_id,
                    request.reason,
                    request.target_zone_or_descriptor,
                    str(request.state),
                ),
            )
