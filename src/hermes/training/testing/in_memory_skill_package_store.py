"""InMemorySkillPackageStore — testing-only (T101, constitución V).

Implementación en memoria de SkillPackagePort: dict por package_id y
lookup por (tenant_id, skill_id, skill_version).

Multi-tenant strict: save y load verifican tenant_id.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from hermes.training.domain.skill_package import SkillPackage
from hermes.training.domain.skill_state import SkillState


class SkillPackageNotFound(RuntimeError):
    """El SkillPackage no existe o no pertenece al tenant."""


class InMemorySkillPackageStore:
    """Store en memoria para tests. Thread-unsafe por diseño (tests sinc)."""

    def __init__(self) -> None:
        # package_id → SkillPackage
        self._by_id: dict[UUID, SkillPackage] = {}

    async def save(self, package: SkillPackage) -> None:
        """Persiste el package. Verifica tenant_id."""
        if package.tenant_id is None:
            raise ValueError("SkillPackage.tenant_id es requerido para save()")
        self._by_id[package.package_id] = package

    async def load(
        self,
        *,
        package_id: UUID,
        tenant_id: UUID,
    ) -> SkillPackage:
        """Carga por ID con verificación de tenant (multi-tenant strict)."""
        pkg = self._by_id.get(package_id)
        if pkg is None or pkg.tenant_id != tenant_id:
            raise SkillPackageNotFound(
                f"SkillPackage {package_id} no encontrado para tenant {tenant_id}"
            )
        return pkg

    async def load_by_skill(
        self,
        *,
        skill_id: UUID,
        tenant_id: UUID,
        skill_version: int | None = None,
    ) -> SkillPackage:
        """Carga el package más reciente por skill_id + tenant.

        Si skill_version se especifica, filtra por esa versión.
        """
        candidates = [
            p
            for p in self._by_id.values()
            if p.skill_id == skill_id and p.tenant_id == tenant_id
        ]
        if skill_version is not None:
            candidates = [p for p in candidates if p.skill_version == skill_version]
        if not candidates:
            raise SkillPackageNotFound(
                f"SkillPackage para skill {skill_id} no encontrado "
                f"(tenant {tenant_id}, version {skill_version})"
            )
        # Retorna la versión más reciente por created_at.
        return max(candidates, key=lambda p: p.created_at)

    async def list_by_tenant(
        self,
        *,
        tenant_id: UUID,
        state: SkillState | None = None,
    ) -> Sequence[SkillPackage]:
        """Lista packages del tenant con filtro opcional de estado."""
        results = [p for p in self._by_id.values() if p.tenant_id == tenant_id]
        if state is not None:
            results = [p for p in results if p.state == state]
        return sorted(results, key=lambda p: p.created_at)

    def all_packages(self) -> list[SkillPackage]:
        """Todos los packages (sin filtro de tenant) — solo para assertions en tests."""
        return list(self._by_id.values())
