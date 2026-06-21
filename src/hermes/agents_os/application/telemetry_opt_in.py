"""TelemetryOptIn — FR-061 BLOQUEANTE (telemetría opt-in pura).

Constitución III + threat-model SURF-TEL-01: el SO NUNCA envía
métricas/logs/traces/data sin consentimiento humano local explícito.

Reglas duras:
  - default OFF en cualquier ISO recién instalada (verificado por
    schema migration 022 + el wizard de first-boot NO marca on por
    defecto).
  - solo el operador humano local puede flipear ON via CLI con TOTP
    validado (`hermes telemetry --enable --confirm`).
  - flip OFF es siempre permitido sin TOTP (fail-safe).
  - cualquier flip se persiste como audit entry firmada
    (AuditKind.TELEMETRY_TOGGLED).
  - la lista de exportadores está enumerada — NO se puede activar un
    exportador desconocido (deny-by-default).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from hermes.agents_os.application.audit_hash_chain import (
    AuditHashChainSigner,
    AuditKind,
)


class TelemetryOptInError(RuntimeError):
    pass


class TotpRequiredError(TelemetryOptInError):
    """FR-061: flip ON requiere TOTP humano local."""


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
    """Gestiona el flag global de telemetría + audit firmada."""

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
        totp_validated: bool,
        exporters: frozenset[TelemetryExporter],
        node_installation_id: UUID | None = None,
    ) -> TelemetryState:
        if not totp_validated:
            raise TotpRequiredError(
                "FR-061: enable requiere TOTP humano local"
            )
        if not exporters:
            raise TelemetryOptInError(
                "exporters vacío — debe declarar al menos uno"
            )
        for exporter in exporters:
            if exporter not in TelemetryExporter:
                raise UnknownExporterError(
                    f"exporter {exporter!r} no enumerado"
                )
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
        # FR-061: disable NO requiere TOTP — fail-safe.
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
