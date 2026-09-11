"""TelemetryOptIn — FR-061 BLOQUEANTE (telemetría opt-in pura).

Constitución III + threat-model SURF-TEL-01: el SO NUNCA envía
métricas/logs/traces/data sin consentimiento humano local explícito.

Reglas duras:
  - default OFF en cualquier ISO recién instalada (verificado por
    schema migration 022 + el wizard de first-boot NO marca on por
    defecto).
  - habilitar requiere propietario autenticado y confirmación explícita, sin MFA.
  - deshabilitar no exige una segunda confirmación (fail-safe).
  - cualquier cambio genera una entrada de auditoría firmada en memoria.
  - la lista de exportadores está enumerada — NO se puede activar un
    exportador desconocido (deny-by-default).

El estado y la cadena de este servicio son en memoria, no un opt-in durable. El
adaptador de entrada debe autenticar al propietario y verificar su confirmación;
el booleano interno no es una credencial ni puede proceder de un payload de agente.
El shell lo construye deshabilitado; no hay ruta de habilitación CLI/UI conectada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from hermes.agents_os.application.audit_hash_chain import (
    AuditHashChainSigner,
    AuditKind,
)


class TelemetryOptInError(RuntimeError):
    pass


class OwnerConfirmationRequiredError(TelemetryOptInError):
    """FR-061: habilitar exige confirmación explícita del propietario local."""


class UnknownExporterError(TelemetryOptInError):
    """Intento de habilitar un exportador no enumerado."""


class TelemetryExporter(StrEnum):
    """Set cerrado de exportadores soportados."""

    PROMETHEUS_PUSH = "prometheus_push"
    OTLP_GRPC_TRACES = "otlp_grpc_traces"
    OTLP_HTTP_LOGS = "otlp_http_logs"


@dataclass(frozen=True, slots=True)
class TelemetryState:
    """Estado actual del opt-in."""

    enabled: bool
    exporters: frozenset[TelemetryExporter]
    enabled_by_human_user_id: UUID | None
    enabled_at: datetime | None
    last_toggle_audit_id: UUID | None
    redact_pii_at_source: bool = True  # invariante FR-061


@dataclass(slots=True)
class TelemetryOptInService:
    """Gestiona el opt-in y audit firmada en memoria, deshabilitado al arrancar."""

    audit_signer: AuditHashChainSigner
    _state: TelemetryState = field(
        default_factory=lambda: TelemetryState(
            enabled=False,
            exporters=frozenset(),
            enabled_by_human_user_id=None,
            enabled_at=None,
            last_toggle_audit_id=None,
        )
    )

    def current(self) -> TelemetryState:
        return self._state

    def enable(
        self,
        *,
        human_user_id: UUID,
        owner_confirmation_validated: bool,
        exporters: frozenset[TelemetryExporter],
        node_installation_id: UUID | None = None,
    ) -> TelemetryState:
        if owner_confirmation_validated is not True or not isinstance(human_user_id, UUID):
            raise OwnerConfirmationRequiredError(
                "FR-061: enable requiere confirmación del propietario local"
            )
        if not exporters:
            raise TelemetryOptInError("exporters vacío — debe declarar al menos uno")
        for exporter in exporters:
            if not isinstance(exporter, TelemetryExporter):
                raise UnknownExporterError(f"exporter {exporter!r} no enumerado")
        audit_entry = self.audit_signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor=str(human_user_id),
            description="telemetry opt-in enabled",
            payload={
                "exporters": sorted(e.value for e in exporters),
                "redact_pii_at_source": True,
            },
            node_installation_id=node_installation_id,
            category="telemetry",
        )
        new = TelemetryState(
            enabled=True,
            exporters=exporters,
            enabled_by_human_user_id=human_user_id,
            enabled_at=datetime.now(tz=UTC),
            last_toggle_audit_id=audit_entry.entry_id,
        )
        self._state = new
        return new

    def disable(
        self,
        *,
        human_user_id: UUID,
        reason: str,
        node_installation_id: UUID | None = None,
    ) -> TelemetryState:
        # FR-061: desactivar no exige una segunda confirmación — fail-safe.
        audit_entry = self.audit_signer.append(
            audit_kind=AuditKind.CONSENT_REVOKED,
            actor=str(human_user_id),
            description=f"telemetry opt-in disabled: {reason}",
            payload={"reason": reason},
            node_installation_id=node_installation_id,
            category="telemetry",
        )
        new = TelemetryState(
            enabled=False,
            exporters=frozenset(),
            enabled_by_human_user_id=None,
            enabled_at=None,
            last_toggle_audit_id=audit_entry.entry_id,
        )
        self._state = new
        return new

    def should_emit(self, exporter: TelemetryExporter) -> bool:
        """Gate sincrono que TODO emisor consulta antes de enviar."""
        return self._state.enabled and exporter in self._state.exporters


class NoopTelemetryExporter:
    """Exportador noop — usado siempre como wrapper.

    Cualquier subsistema (metrics, logs, traces) lo envuelve y consulta
    `should_emit()` antes de pasar el evento al backend real.
    """

    def __init__(
        self,
        *,
        service: TelemetryOptInService,
        exporter_kind: TelemetryExporter,
        backend_emit: Any | None = None,
    ) -> None:
        self._service = service
        self._kind = exporter_kind
        self._backend = backend_emit
        self.dropped_count = 0
        self.emitted_count = 0

    def emit(self, event: dict[str, Any]) -> None:
        if not self._service.should_emit(self._kind):
            self.dropped_count += 1
            return
        if self._backend is None:
            self.emitted_count += 1
            return
        self._backend(event)
        self.emitted_count += 1
