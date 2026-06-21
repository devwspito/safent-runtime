"""InMemoryReplayStore: implementación en memoria del ReplayStore Protocol.

Para tests unitarios. Sin Postgres, sin I/O.

Garantías:
- persist() invalida el activo previo con reason=MANUAL (superseded).
- persist() rechaza signature_hex vacío (ReplayScriptInvalidSignature).
- load_for() devuelve solo el último script activo (invalidated_at IS None).
- invalidate() es idempotente y añade invalidated_at en la copia inmutable.
- history() devuelve todos en orden de inserción (activos + invalidados).

Constitución V: sin Chromium, sin red, sin DB.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from hermes.browser.domain.replay_script import (
    ReplayInvalidationReason,
    ReplayScript,
    ReplayScriptInvalidSignature,
)


class InMemoryReplayStore:
    """Implementación en memoria de ReplayStore para tests."""

    def __init__(self) -> None:
        # script_id -> ReplayScript (including invalidated ones)
        self._scripts: dict[UUID, ReplayScript] = {}
        # insertion order for history()
        self._insertion_order: list[UUID] = []

    async def load_for(
        self,
        *,
        site_id: str,
        flow_id: str,
        tenant_scope: UUID | None,
    ) -> ReplayScript | None:
        """Devuelve el último script activo para la tripleta, o None."""
        for script_id in reversed(self._insertion_order):
            script = self._scripts[script_id]
            if (
                script.site_id == site_id
                and script.flow_id == flow_id
                and script.tenant_scope == tenant_scope
                and script.invalidated_at is None
            ):
                return script
        return None

    async def persist(self, script: ReplayScript) -> None:
        """Guarda script firmado. Invalida el activo previo con MANUAL.

        Raises:
            ReplayScriptInvalidSignature: si signature_hex está vacío.
        """
        if not script.signature_hex:
            raise ReplayScriptInvalidSignature(
                "persist() rechazado: signature_hex vacío — firmar antes de persistir"
            )

        await self._invalidate_active(
            site_id=script.site_id,
            flow_id=script.flow_id,
            tenant_scope=script.tenant_scope,
            reason=ReplayInvalidationReason.MANUAL,
        )

        self._scripts[script.script_id] = script
        self._insertion_order.append(script.script_id)

    async def invalidate(
        self,
        *,
        script_id: UUID,
        reason: ReplayInvalidationReason,
    ) -> None:
        """Marca el script como invalidado. Idempotente."""
        if script_id not in self._scripts:
            return
        script = self._scripts[script_id]
        if script.invalidated_at is not None:
            return
        self._scripts[script_id] = replace(
            script,
            invalidated_at=datetime.now(tz=UTC),
            invalidation_reason=reason,
        )

    async def history(
        self,
        *,
        site_id: str,
        flow_id: str,
        tenant_scope: UUID | None,
    ) -> Sequence[ReplayScript]:
        """Todos los scripts (activos + invalidados) por tripleta."""
        return [
            self._scripts[sid]
            for sid in self._insertion_order
            if (
                self._scripts[sid].site_id == site_id
                and self._scripts[sid].flow_id == flow_id
                and self._scripts[sid].tenant_scope == tenant_scope
            )
        ]

    async def _invalidate_active(
        self,
        *,
        site_id: str,
        flow_id: str,
        tenant_scope: UUID | None,
        reason: ReplayInvalidationReason,
    ) -> None:
        for script_id in list(self._insertion_order):
            script = self._scripts[script_id]
            if (
                script.site_id == site_id
                and script.flow_id == flow_id
                and script.tenant_scope == tenant_scope
                and script.invalidated_at is None
            ):
                self._scripts[script_id] = replace(
                    script,
                    invalidated_at=datetime.now(tz=UTC),
                    invalidation_reason=reason,
                )
