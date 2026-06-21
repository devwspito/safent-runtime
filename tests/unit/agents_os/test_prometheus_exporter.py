"""Tests PrometheusExporterAdapter (FR-061 telemetry-gated)."""

from __future__ import annotations

import secrets
from uuid import uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import (
    AuditHashChainSigner,
)
from hermes.agents_os.application.telemetry_opt_in import (
    TelemetryExporter,
    TelemetryOptInService,
)
from hermes.agents_os.infrastructure.prometheus_exporter import (
    PrometheusExporterAdapter,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def telemetry() -> TelemetryOptInService:
    signer = AuditHashChainSigner(signing_key=secrets.token_bytes(32))
    return TelemetryOptInService(audit_signer=signer)


@pytest.fixture
def exporter(telemetry: TelemetryOptInService) -> PrometheusExporterAdapter:
    return PrometheusExporterAdapter(telemetry=telemetry)


def _enable_telemetry(svc: TelemetryOptInService) -> None:
    svc.enable(
        human_user_id=uuid4(),
        totp_validated=True,
        exporters=frozenset({TelemetryExporter.PROMETHEUS_PUSH}),
    )


class TestGateOff:
    def test_record_dropped_when_off(
        self,
        exporter: PrometheusExporterAdapter,
    ) -> None:
        exporter.record_active_tasks(count=5)
        exporter.record_audit_publish(count=10)
        # Internal counters NO se incrementan cuando off (gated).
        assert exporter.active_tasks_gauge.value() == 0.0
        assert exporter.audit_publish_counter.value() == 0.0
        assert exporter.render_textfile() == ""


class TestGateOn:
    def test_record_passes_when_on(
        self,
        exporter: PrometheusExporterAdapter,
        telemetry: TelemetryOptInService,
    ) -> None:
        _enable_telemetry(telemetry)
        exporter.record_active_tasks(count=5)
        assert exporter.active_tasks_gauge.value() == 5.0

    def test_runtime_state_only_one_active_at_a_time(
        self,
        exporter: PrometheusExporterAdapter,
        telemetry: TelemetryOptInService,
    ) -> None:
        _enable_telemetry(telemetry)
        exporter.record_runtime_state(state="running")
        assert (
            exporter.runtime_state_gauge.value(labels={"state": "running"})
            == 1.0
        )
        assert (
            exporter.runtime_state_gauge.value(labels={"state": "idle"})
            == 0.0
        )

    def test_ota_attempt_counter_increments_per_label(
        self,
        exporter: PrometheusExporterAdapter,
        telemetry: TelemetryOptInService,
    ) -> None:
        _enable_telemetry(telemetry)
        exporter.record_ota_attempt(state="queued")
        exporter.record_ota_attempt(state="queued")
        exporter.record_ota_attempt(
            state="rejected", rejection_reason="downgrade_blocked"
        )
        assert (
            exporter.ota_attempt_counter.value(
                labels={"state": "queued", "rejection_reason": "none"}
            )
            == 2.0
        )
        assert (
            exporter.ota_attempt_counter.value(
                labels={
                    "state": "rejected",
                    "rejection_reason": "downgrade_blocked",
                }
            )
            == 1.0
        )


class TestRender:
    def test_render_includes_all_metrics_when_on(
        self,
        exporter: PrometheusExporterAdapter,
        telemetry: TelemetryOptInService,
    ) -> None:
        _enable_telemetry(telemetry)
        exporter.record_active_tasks(count=2)
        exporter.record_sandbox_count(count=4)
        exporter.record_landlock_apply(capability="documents")
        out = exporter.render_textfile()
        assert "hermes_active_tasks 2.0" in out
        assert "hermes_sandbox_count 4.0" in out
        assert "hermes_landlock_ruleset_apply_total" in out
        assert 'capability="documents"' in out
