"""ReplayStore Protocol: puerto de persistencia para ReplayScript firmado.

El adapter NO valida HMAC — eso lo hace el caller via verify_replay().
El adapter SÍ rechaza scripts con signature_hex vacío en persist().

Implementaciones esperadas:
  - InMemoryReplayStore (tests, hermes-runtime/testing/).
  - PostgresReplayStore (gestoria-agent/adapters/).

Constitución I: este Protocol no filtra hacia arriba nada específico de
Postgres ni de ningún ORM — solo types del dominio.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable
from uuid import UUID

from hermes.browser.domain.replay_script import (
    ReplayInvalidationReason,
    ReplayScript,
    ReplayScriptInvalidSignature,
)

__all__ = [
    "ReplayStore",
    "ReplayScriptInvalidSignature",
    "ReplayInvalidationReason",
]


@runtime_checkable
class ReplayStore(Protocol):
    """Persistencia del ReplayScript firmado.

    Garantías del contrato:
    - persist() exige signature_hex no vacío; si está vacío levanta
      ReplayScriptInvalidSignature.
    - persist() invalida automáticamente el script activo previo para la
      misma (site_id, flow_id, tenant_scope) con reason=SUPERSEDED.
    - load_for() devuelve solo scripts con invalidated_at IS NULL.
    - invalidate() es idempotente: llamar dos veces no levanta.
    - history() incluye scripts invalidados.
    """

    async def load_for(
        self,
        *,
        site_id: str,
        flow_id: str,
        tenant_scope: UUID | None,
    ) -> ReplayScript | None:
        """Último script activo para la tripleta, o None."""
        ...

    async def persist(self, script: ReplayScript) -> None:
        """Guarda el script firmado.

        Si existe un script activo para (site_id, flow_id, tenant_scope),
        lo marca como invalidated con reason=MANUAL (superseded).

        Raises:
            ReplayScriptInvalidSignature: si signature_hex está vacío.
        """
        ...

    async def invalidate(
        self,
        *,
        script_id: UUID,
        reason: ReplayInvalidationReason,
    ) -> None:
        """Marca el script como invalidado. Idempotente."""
        ...

    async def history(
        self,
        *,
        site_id: str,
        flow_id: str,
        tenant_scope: UUID | None,
    ) -> Sequence[ReplayScript]:
        """Todos los scripts (incluidos invalidados) por tripleta."""
        ...
