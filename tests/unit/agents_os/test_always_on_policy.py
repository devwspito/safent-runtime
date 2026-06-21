"""Tests AlwaysOnPolicy — invariante 24/7 (FR-040..FR-046)."""

from __future__ import annotations

import pytest

from hermes.agents_os.domain.always_on_policy import (
    AlwaysOnPolicy,
    InstallProfile,
    default_policy_for,
)

pytestmark = pytest.mark.unit


class TestInvariants:
    def test_screen_lock_pauses_agent_must_be_false(self) -> None:
        """FR-042 invariante: el screen lock NO pausa el agente."""
        with pytest.raises(ValueError, match="FR-042"):
            AlwaysOnPolicy(
                profile=InstallProfile.PERSONAL_DESKTOP,
                screen_lock_pauses_agent=True,
            )

    def test_suspend_targets_validated(self) -> None:
        with pytest.raises(ValueError, match="systemd target válido"):
            AlwaysOnPolicy(
                profile=InstallProfile.SERVER,
                suspend_targets_masked=("not-a-target",),
            )


class TestDefaultPolicy:
    def test_default_includes_all_sleep_targets(self) -> None:
        policy = default_policy_for(InstallProfile.PERSONAL_DESKTOP)
        for required in (
            "sleep.target",
            "suspend.target",
            "hibernate.target",
            "hybrid-sleep.target",
        ):
            assert required in policy.suspend_targets_masked

    def test_default_logind_handles_lid_switch_ignore(self) -> None:
        policy = default_policy_for(InstallProfile.PERSONAL_DESKTOP)
        assert policy.logind_overrides["HandleLidSwitch"] == "ignore"
        assert policy.logind_overrides["IdleAction"] == "ignore"


class TestCriticalServicesPerProfile:
    def test_personal_desktop_excludes_control_plane(self) -> None:
        """En personal-desktop el control plane NO arranca (sin multi-tenant)."""
        policy = default_policy_for(InstallProfile.PERSONAL_DESKTOP)
        services = {s.name for s in policy.services_for_profile()}
        assert "hermes-control-plane.service" not in services
        assert "hermes-runtime.service" in services
        assert "hermes-remote-control.service" in services

    def test_server_includes_control_plane(self) -> None:
        policy = default_policy_for(InstallProfile.SERVER)
        services = {s.name for s in policy.services_for_profile()}
        assert "hermes-control-plane.service" in services

    def test_workspace_only_includes_all_core(self) -> None:
        policy = default_policy_for(InstallProfile.WORKSPACE_ONLY)
        services = {s.name for s in policy.services_for_profile()}
        for required in (
            "hermes-runtime.service",
            "hermes-control-plane.service",
            "hermes-remote-control.service",
            "hermes-whisper.service",
            "hermes-audit-tail.service",
        ):
            assert required in services

    def test_all_critical_services_restart_always(self) -> None:
        policy = default_policy_for(InstallProfile.SERVER)
        for svc in policy.services_for_profile():
            assert svc.restart_policy == "always"
            assert svc.restart_sec <= 10  # FR-043 ≤ 5s default, ≤ 10s margen
