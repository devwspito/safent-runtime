"""Tests TelemetryOptInService (FR-061 BLOQUEANTE)."""

from __future__ import annotations

import secrets
from uuid import uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import (
    AuditHashChainSigner,
)
from hermes.agents_os.application.telemetry_opt_in import (
    NoopTelemetryExporter,
    TelemetryExporter,
    TelemetryOptInError,
    TelemetryOptInService,
    TotpRequiredError,
    UnknownExporterError,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def signer() -> AuditHashChainSigner:
    return AuditHashChainSigner(signing_key=secrets.token_bytes(32))


@pytest.fixture
def service(signer: AuditHashChainSigner) -> TelemetryOptInService:
    return TelemetryOptInService(audit_signer=signer)


class TestDefault:
    def test_default_is_off(
        self, service: TelemetryOptInService
    ) -> None:
        state = service.current()
        assert state.enabled is False
        assert state.exporters == frozenset()
        assert state.enabled_by_human_user_id is None

    def test_should_emit_returns_false_when_off(
        self, service: TelemetryOptInService
    ) -> None:
        assert (
            service.should_emit(TelemetryExporter.PROMETHEUS_PUSH) is False
        )


class TestEnable:
    def test_enable_without_totp_blocked(
        self, service: TelemetryOptInService
    ) -> None:
        with pytest.raises(TotpRequiredError):
            service.enable(
                human_user_id=uuid4(),
                totp_validated=False,
                exporters=frozenset({TelemetryExporter.PROMETHEUS_PUSH}),
            )

    def test_enable_empty_exporters_blocked(
        self, service: TelemetryOptInService
    ) -> None:
        with pytest.raises(TelemetryOptInError):
            service.enable(
                human_user_id=uuid4(),
                totp_validated=True,
                exporters=frozenset(),
            )

    def test_enable_happy_path_persists(
        self, service: TelemetryOptInService
    ) -> None:
        user = uuid4()
        state = service.enable(
            human_user_id=user,
            totp_validated=True,
            exporters=frozenset(
                {
                    TelemetryExporter.PROMETHEUS_PUSH,
                    TelemetryExporter.OTLP_GRPC_TRACES,
                }
            ),
        )
        assert state.enabled is True
        assert state.enabled_by_human_user_id == user
        assert state.last_toggle_audit_id is not None
        assert state.redact_pii_at_source is True

    def test_enable_logs_audit_entry(
        self,
        service: TelemetryOptInService,
        signer: AuditHashChainSigner,
    ) -> None:
        head_before = signer.head_hash_hex
        service.enable(
            human_user_id=uuid4(),
            totp_validated=True,
            exporters=frozenset({TelemetryExporter.PROMETHEUS_PUSH}),
        )
        assert signer.head_hash_hex != head_before


class TestDisable:
    def test_disable_without_totp_allowed(
        self, service: TelemetryOptInService
    ) -> None:
        service.enable(
            human_user_id=uuid4(),
            totp_validated=True,
            exporters=frozenset({TelemetryExporter.PROMETHEUS_PUSH}),
        )
        # FR-061: fail-safe — disable no requiere TOTP.
        state = service.disable(
            human_user_id=uuid4(), reason="user_revoked"
        )
        assert state.enabled is False

    def test_disable_logs_audit(
        self,
        service: TelemetryOptInService,
        signer: AuditHashChainSigner,
    ) -> None:
        service.enable(
            human_user_id=uuid4(),
            totp_validated=True,
            exporters=frozenset({TelemetryExporter.PROMETHEUS_PUSH}),
        )
        head_before = signer.head_hash_hex
        service.disable(human_user_id=uuid4(), reason="x")
        assert signer.head_hash_hex != head_before


class TestNoopExporter:
    def test_emit_dropped_when_off(
        self, service: TelemetryOptInService
    ) -> None:
        backend_calls: list[dict] = []
        exporter = NoopTelemetryExporter(
            service=service,
            exporter_kind=TelemetryExporter.PROMETHEUS_PUSH,
            backend_emit=backend_calls.append,
        )
        exporter.emit({"k": "v"})
        assert exporter.dropped_count == 1
        assert exporter.emitted_count == 0
        assert backend_calls == []

    def test_emit_passes_through_when_on(
        self, service: TelemetryOptInService
    ) -> None:
        service.enable(
            human_user_id=uuid4(),
            totp_validated=True,
            exporters=frozenset({TelemetryExporter.PROMETHEUS_PUSH}),
        )
        backend_calls: list[dict] = []
        exporter = NoopTelemetryExporter(
            service=service,
            exporter_kind=TelemetryExporter.PROMETHEUS_PUSH,
            backend_emit=backend_calls.append,
        )
        exporter.emit({"k": "v"})
        assert exporter.emitted_count == 1
        assert backend_calls == [{"k": "v"}]

    def test_emit_dropped_when_different_exporter_disabled(
        self, service: TelemetryOptInService
    ) -> None:
        # ON solo para PROMETHEUS_PUSH; el de TRACES sigue OFF.
        service.enable(
            human_user_id=uuid4(),
            totp_validated=True,
            exporters=frozenset({TelemetryExporter.PROMETHEUS_PUSH}),
        )
        trace_exporter = NoopTelemetryExporter(
            service=service,
            exporter_kind=TelemetryExporter.OTLP_GRPC_TRACES,
        )
        trace_exporter.emit({"span": "x"})
        assert trace_exporter.dropped_count == 1
