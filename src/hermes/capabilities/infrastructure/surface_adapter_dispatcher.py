"""T040 — SurfaceAdapterDispatcher (CTRL-2/CONSENT-1).

Selecciona el SurfaceAdapterPort correcto por surface_kind y delega
replay(action, hitl_approval_token, consent_token).

Diseño:
- Recibe un dict{SurfaceKind → SurfaceAdapterPort} inyectado en el
  constructor (composición explícita, DIP).
- surface_kind sin adapter ⇒ fail-closed (SurfaceAdapterNotFound).
- NO toca BrowserPort ni modifica SurfaceAdapterPort (Constitución I).
- No evalúa consent ni HITL — eso es responsabilidad exclusiva del broker.

Capa: infrastructure (adapta la colección de ports a un dispatcher
  reutilizable). Sin framework.
"""

from __future__ import annotations

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    SurfaceAdapterPort,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind


class SurfaceAdapterNotFound(KeyError):
    """No hay adapter registrado para este surface_kind — fail-closed."""


class SurfaceAdapterDispatcher:
    """Dispatcher de SurfaceAdapterPort por surface_kind.

    Args:
        adapters: mapa SurfaceKind → SurfaceAdapterPort. Solo los adapters
            explícitamente provistos son alcanzables. Cualquier surface_kind
            no registrado levanta SurfaceAdapterNotFound (fail-closed).
    """

    def __init__(self, *, adapters: dict[SurfaceKind, SurfaceAdapterPort]) -> None:
        self._adapters: dict[SurfaceKind, SurfaceAdapterPort] = dict(adapters)

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        """Selecciona el adapter y delega replay.

        Fail-closed: surface_kind sin adapter ⇒ SurfaceAdapterNotFound
        (no intenta degradar ni adivinar).

        Args:
            action: acción sintetizada por el broker desde proposal.parameters.
            hitl_approval_token: token HITL verificado por el broker (pasado
                al adapter por contrato de SurfaceAdapterPort).
            consent_token: token de consent (pasado al adapter por contrato).

        Raises:
            SurfaceAdapterNotFound: si surface_kind no está en el mapa.
        """
        adapter = self._resolve(action.surface_kind)
        return await adapter.replay(
            action,
            hitl_approval_token=hitl_approval_token,
            consent_token=consent_token,
        )

    def registered_kinds(self) -> frozenset[SurfaceKind]:
        """Devuelve el conjunto de SurfaceKind registrados (observabilidad)."""
        return frozenset(self._adapters)

    def _resolve(self, kind: SurfaceKind) -> SurfaceAdapterPort:
        adapter = self._adapters.get(kind)
        if adapter is None:
            raise SurfaceAdapterNotFound(
                f"No hay SurfaceAdapter registrado para surface_kind={kind!r}. "
                "Registra el adapter en la composición raíz del proceso. "
                "(Constitución I: BrowserPort no se toca; Constitución IV: fail-closed)."
            )
        return adapter
