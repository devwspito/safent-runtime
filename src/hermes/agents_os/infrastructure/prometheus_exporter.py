"""PrometheusExporterAdapter — observabilidad gateada por telemetry opt-in.

Spec 003 FR-061. Exporta Prometheus metrics SOLO si
`TelemetryOptInService.should_emit(PROMETHEUS_PUSH) == True`.

En CI base no requiere `prometheus_client`; fallback a contadores
internos. En producción se carga lazy.

Métricas mínimas:
  - hermes_runtime_state{state}                       gauge
  - hermes_active_tasks                                gauge
  - hermes_sandbox_count                               gauge
  - hermes_audit_publish_total                         counter
  - hermes_ota_attempt_total{state, rejection_reason}  counter
  - hermes_remote_control_active_sessions              gauge
  - hermes_landlock_ruleset_apply_total{capability}    counter
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from hermes.agents_os.application.telemetry_opt_in import (
    TelemetryExporter,
    TelemetryOptInService,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _InternalCounter:
    """Contador interno usable sin prometheus_client."""

    name: str
    samples: dict[tuple[tuple[str, str], ...], float] = field(
        default_factory=dict
    )

    def inc(self, labels: dict[str, str] | None = None, *, amount: float = 1.0) -> None:
        key = tuple(sorted((labels or {}).items()))
        self.samples[key] = self.samples.get(key, 0.0) + amount

    def value(self, labels: dict[str, str] | None = None) -> float:
        key = tuple(sorted((labels or {}).items()))
        return self.samples.get(key, 0.0)


@dataclass(slots=True)
class _InternalGauge(_InternalCounter):
    def set(self, value: float, *, labels: dict[str, str] | None = None) -> None:
        key = tuple(sorted((labels or {}).items()))
        self.samples[key] = float(value)


@dataclass(slots=True)
class PrometheusExporterAdapter:
    """Exporter con gate de opt-in."""

    telemetry: TelemetryOptInService
    runtime_state_gauge: _InternalGauge = field(
        default_factory=lambda: _InternalGauge("hermes_runtime_state")
    )
    active_tasks_gauge: _InternalGauge = field(
        default_factory=lambda: _InternalGauge("hermes_active_tasks")
    )
    sandbox_count_gauge: _InternalGauge = field(
        default_factory=lambda: _InternalGauge("hermes_sandbox_count")
    )
    audit_publish_counter: _InternalCounter = field(
        default_factory=lambda: _InternalCounter("hermes_audit_publish_total")
    )
    ota_attempt_counter: _InternalCounter = field(
        default_factory=lambda: _InternalCounter("hermes_ota_attempt_total")
    )
    remote_control_gauge: _InternalGauge = field(
        default_factory=lambda: _InternalGauge(
            "hermes_remote_control_active_sessions"
        )
    )
    landlock_apply_counter: _InternalCounter = field(
        default_factory=lambda: _InternalCounter(
            "hermes_landlock_ruleset_apply_total"
        )
    )

    def _gated(self) -> bool:
        return self.telemetry.should_emit(TelemetryExporter.PROMETHEUS_PUSH)

    def record_runtime_state(self, *, state: str) -> None:
        if not self._gated():
            return
        # All states reset to 0, current to 1 (gauge convention).
        for known in ("idle", "running", "paused", "unknown"):
            self.runtime_state_gauge.set(
                1.0 if known == state else 0.0, labels={"state": known}
            )

    def record_active_tasks(self, *, count: int) -> None:
        if not self._gated():
            return
        self.active_tasks_gauge.set(float(count))

    def record_sandbox_count(self, *, count: int) -> None:
        if not self._gated():
            return
        self.sandbox_count_gauge.set(float(count))

    def record_audit_publish(self, *, count: int = 1) -> None:
        if not self._gated():
            return
        self.audit_publish_counter.inc(amount=float(count))

    def record_ota_attempt(
        self, *, state: str, rejection_reason: str | None = None
    ) -> None:
        if not self._gated():
            return
        labels = {"state": state, "rejection_reason": rejection_reason or "none"}
        self.ota_attempt_counter.inc(labels)

    def record_remote_control_active(self, *, count: int) -> None:
        if not self._gated():
            return
        self.remote_control_gauge.set(float(count))

    def record_landlock_apply(self, *, capability: str) -> None:
        if not self._gated():
            return
        self.landlock_apply_counter.inc({"capability": capability})

    def render_textfile(self) -> str:
        """Genera el output text del exporter (formato Prometheus)."""
        if not self._gated():
            return ""
        lines: list[str] = []
        for metric in (
            self.runtime_state_gauge,
            self.active_tasks_gauge,
            self.sandbox_count_gauge,
            self.audit_publish_counter,
            self.ota_attempt_counter,
            self.remote_control_gauge,
            self.landlock_apply_counter,
        ):
            for label_tuple, value in metric.samples.items():
                if label_tuple:
                    label_str = ",".join(f'{k}="{v}"' for k, v in label_tuple)
                    lines.append(f"{metric.name}{{{label_str}}} {value}")
                else:
                    lines.append(f"{metric.name} {value}")
        return "\n".join(lines) + ("\n" if lines else "")
