"""SQLiteSkillPackageRepo — variante personal-desktop.

Migration 001 SQLite ya creó `skill_packages`. Aquí persistimos los
SkillPackage producidos por SkillCompiler.

NOTA: el adapter NO recrea el signature_hex — solo lo persiste tal
cual el compiler lo emitió. Al cargar de disco devolvemos el
SkillPackage idéntico para que `SkillCompiler.verify()` siga
validando.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from hermes.agents_os.application.skill_compiler import (
    SkillPackage,
    SkillPackageState,
    SkillStep,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind


class SQLiteSkillPackageRepo:
    """Persistencia SQLite single-tenant."""

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            isolation_level=None,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, package: SkillPackage) -> None:
        steps_payload = {
            sk: [
                {
                    "sequence_index": step.sequence_index,
                    "surface_kind": step.surface_kind.value,
                    "action_payload": step.action_payload,
                }
                for step in steps
            ]
            for sk, steps in package.steps_by_surface_kind.items()
        }
        surface_kinds = sorted(sk.value for sk in package.surface_kinds)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO skill_packages (
                  package_id, tenant_id, skill_id, skill_version,
                  state, signature_hex, surface_kinds, cross_domain,
                  steps_by_surface_kind, intent_caption,
                  source_training_session_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(package.package_id),
                    str(package.tenant_id),
                    package.skill_id,
                    package.version,
                    package.state.value,
                    package.signature_hex,
                    json.dumps(surface_kinds),
                    1 if package.cross_domain else 0,
                    json.dumps(steps_payload),
                    package.intent_caption,
                    str(package.source_training_session_id),
                    package.created_at.isoformat(),
                ),
            )

    def deprecate(self, *, package_id: UUID) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE skill_packages SET state = ? WHERE package_id = ?",
                (SkillPackageState.DEPRECATED.value, str(package_id)),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown package_id {package_id}")

    def list_versions(
        self, *, tenant_id: UUID, skill_id: str
    ) -> list[SkillPackage]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM skill_packages
                WHERE tenant_id = ? AND skill_id = ?
                ORDER BY skill_version ASC
                """,
                (str(tenant_id), skill_id),
            ).fetchall()
        return [_row_to_package(r) for r in rows]


def _row_to_package(row) -> SkillPackage:
    steps_raw = json.loads(row["steps_by_surface_kind"])
    steps_by_surface: dict[str, list[SkillStep]] = {}
    for sk, items in steps_raw.items():
        steps_by_surface[sk] = [
            SkillStep(
                sequence_index=item["sequence_index"],
                surface_kind=SurfaceKind(item["surface_kind"]),
                action_payload=item["action_payload"],
            )
            for item in items
        ]
    surface_kinds = frozenset(
        SurfaceKind(s) for s in json.loads(row["surface_kinds"])
    )
    return SkillPackage(
        package_id=UUID(row["package_id"]),
        tenant_id=UUID(row["tenant_id"]),
        skill_id=row["skill_id"],
        version=row["skill_version"],
        state=SkillPackageState(row["state"]),
        surface_kinds=surface_kinds,
        cross_domain=bool(row["cross_domain"]),
        steps_by_surface_kind=steps_by_surface,
        intent_caption=row["intent_caption"] if "intent_caption" in row.keys() else "",
        source_training_session_id=(
            UUID(row["source_training_session_id"])
            if "source_training_session_id" in row.keys()
            and row["source_training_session_id"]
            else UUID(row["package_id"])
        ),
        created_at=datetime.fromisoformat(row["created_at"]),
        signature_hex=row["signature_hex"],
    )
