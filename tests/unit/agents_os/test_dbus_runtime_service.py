"""Tests InMemoryRuntimeService (contrato org.hermes.Runtime1)."""

from __future__ import annotations

import secrets
from uuid import uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import (
    AuditHashChainSigner,
)
from hermes.agents_os.application.consent_manager import Capability
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    InMemoryRuntimeService,
    RuntimeStateError,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def signer() -> AuditHashChainSigner:
    return AuditHashChainSigner(signing_key=secrets.token_bytes(32))


@pytest.fixture
def svc(signer: AuditHashChainSigner) -> InMemoryRuntimeService:
    return InMemoryRuntimeService(audit_signer=signer)


class TestStatus:
    def test_initial_status_idle(self, svc: InMemoryRuntimeService) -> None:
        s = svc.get_status()
        assert s.state == "idle"
        assert s.active_task_count == 0
        assert s.telemetry_enabled is False

    def test_status_after_task_started(
        self, svc: InMemoryRuntimeService
    ) -> None:
        svc.mark_task_started()
        s = svc.get_status()
        assert s.state == "running"
        assert s.active_task_count == 1

    def test_status_returns_to_idle_after_completed(
        self, svc: InMemoryRuntimeService
    ) -> None:
        svc.mark_task_started()
        svc.mark_task_completed()
        assert svc.get_status().state == "idle"

    def test_status_audit_head_reflects_signer(
        self, svc: InMemoryRuntimeService, signer: AuditHashChainSigner
    ) -> None:
        from hermes.agents_os.application.audit_hash_chain import AuditKind

        signer.append(
            audit_kind=AuditKind.CONSENT_GRANTED,
            actor="u",
            description="x",
            payload={},
        )
        assert svc.get_status().last_audit_head_hex == signer.head_hash_hex


class TestPauseResume:
    def test_pause_then_resume(self, svc: InMemoryRuntimeService) -> None:
        user = uuid4()
        svc.request_pause(reason="manual", authorizing_user_id=user)
        assert svc.get_status().state == "paused"
        svc.request_resume(authorizing_user_id=user)
        assert svc.get_status().state == "idle"

    def test_pause_idempotent(self, svc: InMemoryRuntimeService) -> None:
        user = uuid4()
        svc.request_pause(reason="m", authorizing_user_id=user)
        svc.request_pause(reason="m", authorizing_user_id=user)
        assert svc.get_status().state == "paused"

    def test_resume_when_not_paused_raises(
        self, svc: InMemoryRuntimeService
    ) -> None:
        with pytest.raises(RuntimeStateError):
            svc.request_resume(authorizing_user_id=uuid4())


class TestConsents:
    def test_add_consent_visible_in_get(
        self, svc: InMemoryRuntimeService
    ) -> None:
        svc.add_consent(Capability.DOCUMENTS)
        svc.add_consent(Capability.TERMINAL)
        assert set(svc.get_active_consents()) == {
            Capability.DOCUMENTS,
            Capability.TERMINAL,
        }

    def test_revoke_consent_removes(
        self, svc: InMemoryRuntimeService
    ) -> None:
        svc.add_consent(Capability.DOCUMENTS)
        svc.revoke_consent(Capability.DOCUMENTS)
        assert svc.get_active_consents() == ()


class TestTelemetry:
    def test_telemetry_default_off(
        self, svc: InMemoryRuntimeService
    ) -> None:
        assert svc.get_status().telemetry_enabled is False

    def test_telemetry_flip_on(
        self, svc: InMemoryRuntimeService
    ) -> None:
        svc.set_telemetry_enabled(value=True, authorizing_user_id=uuid4())
        assert svc.get_status().telemetry_enabled is True


class TestSandbox:
    def test_set_sandbox_count(
        self, svc: InMemoryRuntimeService
    ) -> None:
        svc.set_sandbox_count(3)
        assert svc.get_status().sandbox_count == 3

    def test_set_sandbox_negative_raises(
        self, svc: InMemoryRuntimeService
    ) -> None:
        with pytest.raises(ValueError):
            svc.set_sandbox_count(-1)
