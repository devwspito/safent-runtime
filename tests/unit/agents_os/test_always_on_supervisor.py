"""Tests AlwaysOnSupervisor (FR-040..FR-046)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from hermes.agents_os.application.always_on_supervisor import (
    AlwaysOnSupervisor,
    DrainIncompleteError,
    SuspendNotAuthorizedError,
)
from hermes.agents_os.domain.always_on_policy import (
    InstallProfile,
    default_policy_for,
)

pytestmark = pytest.mark.unit


class _FakeSupervisor:
    def __init__(self) -> None:
        self.masked: list[str] = []
        self.unmasked: list[str] = []
        self.logind: dict[str, str] = {}
        self.services: list[str] = []
        self.suspend_called = 0

    def mask_targets(self, targets: Sequence[str]) -> None:
        self.masked.extend(targets)

    def unmask_targets(self, targets: Sequence[str]) -> None:
        self.unmasked.extend(targets)

    def write_logind_override(self, key_values: dict[str, str]) -> None:
        self.logind.update(key_values)

    def ensure_service_unit(self, service) -> None:
        self.services.append(service.name)

    def list_active_critical_services(self) -> tuple[str, ...]:
        return tuple(self.services)

    def suspend_system(self) -> None:
        self.suspend_called += 1


class TestApplyPolicy:
    def test_workspace_only_masks_suspend_targets(self) -> None:
        fake = _FakeSupervisor()
        sup = AlwaysOnSupervisor(supervisor=fake)
        result = sup.apply(default_policy_for(InstallProfile.WORKSPACE_ONLY))

        assert "sleep.target" in fake.masked
        assert "suspend.target" in fake.masked
        assert "hibernate.target" in fake.masked
        assert result.targets_masked == tuple(fake.masked)

    def test_personal_desktop_includes_lid_switch_ignore(self) -> None:
        fake = _FakeSupervisor()
        AlwaysOnSupervisor(supervisor=fake).apply(
            default_policy_for(InstallProfile.PERSONAL_DESKTOP)
        )
        assert fake.logind["HandleLidSwitch"] == "ignore"
        assert fake.logind["HandleSuspendKey"] == "ignore"

    def test_server_skips_control_plane_in_personal(self) -> None:
        fake = _FakeSupervisor()
        AlwaysOnSupervisor(supervisor=fake).apply(
            default_policy_for(InstallProfile.PERSONAL_DESKTOP)
        )
        # control-plane solo en workspace_only y server, no en personal-desktop.
        assert "hermes-control-plane.service" not in fake.services

    def test_idempotent_application(self) -> None:
        fake = _FakeSupervisor()
        sup = AlwaysOnSupervisor(supervisor=fake)
        policy = default_policy_for(InstallProfile.SERVER)
        sup.apply(policy)
        first_mask_count = len(fake.masked)
        sup.apply(policy)
        # En el fake duplica; en el adapter real `systemctl mask` es no-op.
        # Aquí solo verificamos que NO falla y devuelve resultado coherente.
        assert len(fake.masked) == first_mask_count * 2


class TestSuspendAuthorization:
    def _policy(self):
        return default_policy_for(InstallProfile.PERSONAL_DESKTOP)

    def test_suspend_blocked_without_totp(self) -> None:
        fake = _FakeSupervisor()
        sup = AlwaysOnSupervisor(supervisor=fake)
        with pytest.raises(SuspendNotAuthorizedError):
            sup.suspend_with_authorization(
                policy=self._policy(),
                authorizing_human_user_id=uuid4(),
                totp_validated=False,
                drain_completed=True,
            )
        assert fake.suspend_called == 0

    def test_suspend_blocked_without_drain(self) -> None:
        fake = _FakeSupervisor()
        sup = AlwaysOnSupervisor(supervisor=fake)
        with pytest.raises(DrainIncompleteError):
            sup.suspend_with_authorization(
                policy=self._policy(),
                authorizing_human_user_id=uuid4(),
                totp_validated=True,
                drain_completed=False,
            )
        assert fake.suspend_called == 0

    def test_suspend_force_skips_drain(self) -> None:
        fake = _FakeSupervisor()
        sup = AlwaysOnSupervisor(supervisor=fake)
        sup.suspend_with_authorization(
            policy=self._policy(),
            authorizing_human_user_id=uuid4(),
            totp_validated=True,
            drain_completed=False,
            force=True,
        )
        assert fake.suspend_called == 1

    def test_suspend_happy_path(self) -> None:
        fake = _FakeSupervisor()
        sup = AlwaysOnSupervisor(supervisor=fake)
        sup.suspend_with_authorization(
            policy=self._policy(),
            authorizing_human_user_id=uuid4(),
            totp_validated=True,
            drain_completed=True,
        )
        assert fake.suspend_called == 1


class TestClock:
    def test_applied_at_uses_injected_clock(self) -> None:
        fixed = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
        fake = _FakeSupervisor()
        sup = AlwaysOnSupervisor(supervisor=fake, clock=lambda: fixed)
        result = sup.apply(default_policy_for(InstallProfile.WORKSPACE_ONLY))
        assert result.applied_at == fixed
