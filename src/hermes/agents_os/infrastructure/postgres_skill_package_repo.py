"""PostgresSkillPackageRepo — server multi-tenant.

Migration 018 + 023 (intent_caption + source_training_session_id).
asyncpg directo. La interfaz EXTERNA es síncrona para coincidir con
`SkillPackageRepoPort` consumido por `IntentRouter` (sin asyncio en
el path crítico de replay del runtime).

El método interno usa el pool async; un wrapper `asyncio.run`
puente cuando es necesario, pero la mayoría de los call sites son
sync (CLI, agentic panel, replay).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from hermes.agents_os.application.skill_compiler import (
    SkillPackage,
    SkillPackageState,
    SkillStep,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind


class PostgresSkillPackageRepo:
    """Repo Postgres con pool asyncpg compatible."""

    def __init__(self, *, pool) -> None:
        self._pool = pool

    async def add_async(self, package: SkillPackage) -> None:
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
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agents_os.skill_packages (
                  package_id, tenant_id, skill_id, skill_version,
                  state, signature_hex, surface_kinds, cross_domain,
                  steps_by_surface_kind, intent_caption,
                  source_training_session_id, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                package.package_id,
                package.tenant_id,
                package.skill_id,
                package.version,
                package.state.value,
                package.signature_hex,
                surface_kinds,
                package.cross_domain,
                json.dumps(steps_payload),
                package.intent_caption,
                package.source_training_session_id,
                package.created_at,
            )

    async def deprecate_async(self, *, package_id: UUID) -> None:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE agents_os.skill_packages SET state = $1 "
                "WHERE package_id = $2",
                SkillPackageState.DEPRECATED.value,
                package_id,
            )
            if result.endswith("0"):
                raise KeyError(f"unknown package_id {package_id}")

    async def list_versions_async(
        self, *, tenant_id: UUID, skill_id: str
    ) -> list[SkillPackage]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT package_id, tenant_id, skill_id, skill_version,
                       state, signature_hex, surface_kinds, cross_domain,
                       steps_by_surface_kind, intent_caption,
                       source_training_session_id, created_at
                FROM agents_os.skill_packages
                WHERE tenant_id = $1 AND skill_id = $2
                ORDER BY skill_version ASC
                """,
                tenant_id,
                skill_id,
            )
        return [_row_to_package(r) for r in rows]

    # Wrappers síncronos para SkillPackageRepoPort.

    def add(self, package: SkillPackage) -> None:
        asyncio.run(self.add_async(package))

    def deprecate(self, *, package_id: UUID) -> None:
        asyncio.run(self.deprecate_async(package_id=package_id))

    def list_versions(
        self, *, tenant_id: UUID, skill_id: str
    ) -> list[SkillPackage]:
        return asyncio.run(
            self.list_versions_async(tenant_id=tenant_id, skill_id=skill_id)
        )


def _row_to_package(row: dict[str, Any]) -> SkillPackage:
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
    return SkillPackage(
        package_id=row["package_id"],
        tenant_id=row["tenant_id"],
        skill_id=row["skill_id"],
        version=row["skill_version"],
        state=SkillPackageState(row["state"]),
        surface_kinds=frozenset(
            SurfaceKind(s) for s in row["surface_kinds"]
        ),
        cross_domain=row["cross_domain"],
        steps_by_surface_kind=steps_by_surface,
        intent_caption=row["intent_caption"],
        source_training_session_id=row["source_training_session_id"]
        or row["package_id"],
        created_at=row["created_at"]
        if isinstance(row["created_at"], datetime)
        else datetime.fromisoformat(row["created_at"]),
        signature_hex=row["signature_hex"],
    )
