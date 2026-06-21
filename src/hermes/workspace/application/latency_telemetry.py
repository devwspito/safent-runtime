"""LatencyTelemetry — detector de latencia inicial del Workspace (T094).

Mide el round-trip al inicio de la sesión vía Selkies ICE stats.
Si > 120ms: marca todos los StepRecords con latency_warning=True y emite
aviso al panel via WS channel.

Ref: FR-008, NFR-001, edge case "latencia alta".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

logger = logging.getLogger(__name__)

_LATENCY_WARNING_THRESHOLD_MS = 120


class LatencyWarningChannel(Protocol):
    """Canal WS para emitir aviso de latencia al panel del formador."""

    async def send_latency_warning(
        self,
        *,
        workspace_id: UUID,
        tenant_id: UUID,
        rtt_ms: float,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class LatencyMeasurement:
    workspace_id: UUID
    tenant_id: UUID
    rtt_ms: float
    exceeds_threshold: bool

    @classmethod
    def from_rtt(
        cls,
        workspace_id: UUID,
        tenant_id: UUID,
        rtt_ms: float,
    ) -> "LatencyMeasurement":
        return cls(
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            rtt_ms=rtt_ms,
            exceeds_threshold=rtt_ms > _LATENCY_WARNING_THRESHOLD_MS,
        )


class LatencyTelemetry:
    """Registra latencia inicial y emite aviso cuando supera el umbral."""

    def __init__(self, *, channel: LatencyWarningChannel) -> None:
        self._channel = channel

    async def measure_and_warn(
        self,
        *,
        workspace_id: UUID,
        tenant_id: UUID,
        rtt_ms: float,
    ) -> LatencyMeasurement:
        """Mide RTT y emite aviso si supera 120ms.

        Retorna LatencyMeasurement con el flag exceeds_threshold para que el
        TrainingOrchestrator marque los StepRecords con latency_warning=True.
        """
        measurement = LatencyMeasurement.from_rtt(workspace_id, tenant_id, rtt_ms)

        if measurement.exceeds_threshold:
            logger.warning(
                "workspace_latency_high",
                extra={
                    "workspace_id": str(workspace_id),
                    "tenant_id": str(tenant_id),
                    "rtt_ms": rtt_ms,
                    "threshold_ms": _LATENCY_WARNING_THRESHOLD_MS,
                },
            )
            await self._channel.send_latency_warning(
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                rtt_ms=rtt_ms,
            )
        else:
            logger.info(
                "workspace_latency_ok",
                extra={
                    "workspace_id": str(workspace_id),
                    "tenant_id": str(tenant_id),
                    "rtt_ms": rtt_ms,
                },
            )

        return measurement
