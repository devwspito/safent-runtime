"""Regression tests: PrometheusExporterAdapter (finding #26).

Verifies that:
1. The adapter is instantiated and records metrics (not a dead no-op).
2. render_textfile() returns valid Prometheus text format when telemetry is enabled.
3. When telemetry is disabled (default), render_textfile() returns empty string.
"""

from __future__ import annotations

import secrets

import pytest

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
from hermes.agents_os.application.telemetry_opt_in import (
    TelemetryExporter,
    TelemetryOptInService,
)
from hermes.agents_os.infrastructure.prometheus_exporter import (
    PrometheusExporterAdapter,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def signer() -> AuditHashChainSigner:
    return AuditHashChainSigner(signing_key=secrets.token_bytes(32))


@pytest.fixture
def telemetry_off(signer: AuditHashChainSigner) -> TelemetryOptInService:
    return TelemetryOptInService(audit_signer=signer)


@pytest.fixture
def telemetry_on(signer: AuditHashChainSigner) -> TelemetryOptInService:
    from uuid import uuid4

    svc = TelemetryOptInService(audit_signer=signer)
    svc.enable(
        human_user_id=uuid4(),
        totp_validated=True,
        exporters=frozenset([TelemetryExporter.PROMETHEUS_PUSH]),
    )
    return svc


class TestPrometheusExporterAdapter:
    def test_render_empty_when_telemetry_off(
        self, telemetry_off: TelemetryOptInService
    ) -> None:
        adapter = PrometheusExporterAdapter(telemetry=telemetry_off)
        adapter.record_runtime_state(state="idle")
        # Gate is off — render must return empty string.
        assert adapter.render_textfile() == ""

    def test_render_nonempty_when_telemetry_on(
        self, telemetry_on: TelemetryOptInService
    ) -> None:
        adapter = PrometheusExporterAdapter(telemetry=telemetry_on)
        adapter.record_runtime_state(state="running")
        output = adapter.render_textfile()
        assert "hermes_runtime_state" in output
        assert 'state="running"' in output
        assert "1.0" in output  # current state = 1.0

    def test_active_tasks_recorded(
        self, telemetry_on: TelemetryOptInService
    ) -> None:
        adapter = PrometheusExporterAdapter(telemetry=telemetry_on)
        adapter.record_active_tasks(count=3)
        output = adapter.render_textfile()
        assert "hermes_active_tasks" in output
        assert "3.0" in output

    def test_audit_publish_counter_increments(
        self, telemetry_on: TelemetryOptInService
    ) -> None:
        adapter = PrometheusExporterAdapter(telemetry=telemetry_on)
        adapter.record_audit_publish(count=5)
        adapter.record_audit_publish(count=3)
        assert (
            adapter.audit_publish_counter.value() == 8.0
        )

    def test_landlock_apply_counter_per_capability(
        self, telemetry_on: TelemetryOptInService
    ) -> None:
        adapter = PrometheusExporterAdapter(telemetry=telemetry_on)
        adapter.record_landlock_apply(capability="documents")
        adapter.record_landlock_apply(capability="documents")
        adapter.record_landlock_apply(capability="microphone")
        output = adapter.render_textfile()
        assert "hermes_landlock_ruleset_apply_total" in output
