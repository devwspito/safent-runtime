"""IntentRouter — selecciona SkillPackage por intent.

Spec 003 FR-028 — cuando el runtime decide "ejecutar la skill X",
necesita el SkillPackage SIGNED más reciente de version monotónica
mayor para el (tenant_id, skill_id) dado, asegurando que NO se
ejecute una versión deprecada.

Política:
  - Selecciona la version más alta NON-deprecated.
  - Si la última está DEPRECATED, NUNCA hace fallback a versions
    anteriores — la deprecación es señal de "no usar más".
  - Si no hay SIGNED versions, raise SkillNotAvailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from hermes.agents_os.application.skill_compiler import (
    SkillPackage,
    SkillPackageState,
)


class SkillNotAvailable(RuntimeError):
    pass


class SkillDeprecated(SkillNotAvailable):
    """La última version está DEPRECATED — no fallback."""


@runtime_checkable
class SkillPackageRepoPort(Protocol):
    """Repo de SkillPackages (Postgres en server, SQLite en personal)."""

    def list_versions(
        self, *, tenant_id: UUID, skill_id: str
    ) -> list[SkillPackage]: ...


@dataclass(slots=True)
class IntentRouter:
    """Selecciona la SkillPackage adecuada por (tenant, skill_id)."""

    repo: SkillPackageRepoPort

    def resolve(
        self, *, tenant_id: UUID, skill_id: str
    ) -> SkillPackage:
        versions = self.repo.list_versions(
            tenant_id=tenant_id, skill_id=skill_id
        )
        if not versions:
            raise SkillNotAvailable(
                f"no hay SkillPackage para ({tenant_id}, {skill_id})"
            )
        latest = max(versions, key=lambda p: p.version)
        if latest.state == SkillPackageState.DEPRECATED:
            raise SkillDeprecated(
                f"última version de ({tenant_id}, {skill_id}) está DEPRECATED"
            )
        if latest.state != SkillPackageState.SIGNED:
            raise SkillNotAvailable(
                f"última version está en estado {latest.state}"
            )
        return latest


class InMemorySkillPackageRepo:
    """Repo in-memory para tests."""

    def __init__(self) -> None:
        self._packages: list[SkillPackage] = []

    def add(self, package: SkillPackage) -> None:
        self._packages.append(package)

    def deprecate(self, package_id: UUID) -> None:
        from dataclasses import replace

        for i, p in enumerate(self._packages):
            if p.package_id == package_id:
                self._packages[i] = replace(
                    p, state=SkillPackageState.DEPRECATED
                )

    def list_versions(
        self, *, tenant_id: UUID, skill_id: str
    ) -> list[SkillPackage]:
        return [
            p
            for p in self._packages
            if p.tenant_id == tenant_id and p.skill_id == skill_id
        ]
