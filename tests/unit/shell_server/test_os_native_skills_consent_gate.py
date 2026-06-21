"""Regression tests: OS-native skills consent pre-flight (finding #3).

Before the fix:
  - _check_consent with consent_manager=None or human_operator_id=None
    would silently return (fail-open) in ALL profiles, including personal-desktop.
  - The consent gate was effectively dead code in production.

After the fix:
  - In personal-desktop profile: None args raise ConsentDenied (fail-closed).
  - In non-personal-desktop (headless/test): None args are allowed through.
  - In personal-desktop with real consent: gate passes for granted caps.
"""

from __future__ import annotations

import os
from unittest.mock import mock_open, patch
from uuid import uuid4

import pytest

from hermes.agents_os.application.consent_manager import (
    Capability,
    ConsentDenied,
    ConsentManager,
    ConsentScope,
)
from hermes.shell_server.os_native_skills.tool_specs import (
    _check_consent,
    _is_personal_desktop_profile,
)

pytestmark = pytest.mark.unit


class TestIsPersonalDesktopProfile:
    def test_returns_true_when_file_says_personal_desktop(self) -> None:
        with patch(
            "builtins.open",
            mock_open(read_data="personal-desktop"),
        ):
            assert _is_personal_desktop_profile() is True

    def test_returns_false_when_file_absent(self) -> None:
        with patch("builtins.open", side_effect=OSError("no such file")):
            assert _is_personal_desktop_profile() is False

    def test_returns_false_for_server_profile(self) -> None:
        with patch("builtins.open", mock_open(read_data="server")):
            assert _is_personal_desktop_profile() is False


class TestCheckConsentFailClosed:
    def test_none_args_silent_in_headless(self) -> None:
        """Outside personal-desktop: None consent_manager is allowed (headless/test)."""
        with patch(
            "hermes.shell_server.os_native_skills.tool_specs._is_personal_desktop_profile",
            return_value=False,
        ):
            # Must NOT raise.
            _check_consent(
                required_capabilities=("screen",),
                consent_manager=None,
                human_operator_id=None,
                skill_name="screenshot",
            )

    def test_none_consent_manager_raises_on_personal_desktop(self) -> None:
        """On personal-desktop: None consent_manager raises ConsentDenied (fail-closed)."""
        with patch(
            "hermes.shell_server.os_native_skills.tool_specs._is_personal_desktop_profile",
            return_value=True,
        ):
            with pytest.raises(ConsentDenied, match="Consent gate not wired"):
                _check_consent(
                    required_capabilities=("screen",),
                    consent_manager=None,
                    human_operator_id=None,
                    skill_name="screenshot",
                )

    def test_none_operator_raises_on_personal_desktop(self) -> None:
        """Even with a real ConsentManager, missing operator raises on personal-desktop."""
        mgr = ConsentManager()
        with patch(
            "hermes.shell_server.os_native_skills.tool_specs._is_personal_desktop_profile",
            return_value=True,
        ):
            with pytest.raises(ConsentDenied):
                _check_consent(
                    required_capabilities=("screen",),
                    consent_manager=mgr,
                    human_operator_id=None,
                    skill_name="screenshot",
                )

    def test_valid_consent_passes_gate(self) -> None:
        """With a granted consent, _check_consent does not raise."""
        op = uuid4()
        ten = uuid4()
        mgr = ConsentManager()
        mgr.grant(
            tenant_id=ten,
            human_operator_id=op,
            capability=Capability.SCREEN_CAPTURE,
            scope=ConsentScope.SESSION,
        )
        with patch(
            "hermes.shell_server.os_native_skills.tool_specs._is_personal_desktop_profile",
            return_value=True,
        ):
            # Must NOT raise.
            _check_consent(
                required_capabilities=("screen",),
                consent_manager=mgr,
                human_operator_id=op,
                skill_name="screenshot",
            )

    def test_missing_capability_raises_denied(self) -> None:
        """If SCREEN_CAPTURE is not granted, gate raises ConsentDenied."""
        op = uuid4()
        mgr = ConsentManager()
        # Grant something else, not screen.
        mgr.grant(
            tenant_id=uuid4(),
            human_operator_id=op,
            capability=Capability.TERMINAL,
            scope=ConsentScope.SESSION,
        )
        with patch(
            "hermes.shell_server.os_native_skills.tool_specs._is_personal_desktop_profile",
            return_value=True,
        ):
            with pytest.raises(ConsentDenied):
                _check_consent(
                    required_capabilities=("screen",),
                    consent_manager=mgr,
                    human_operator_id=op,
                    skill_name="screenshot",
                )
