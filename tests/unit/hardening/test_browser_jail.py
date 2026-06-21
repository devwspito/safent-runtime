"""Unit tests for hermes.security.browser_jail.

Covers:
  - _jail_enabled: env var control.
  - build_jailed_argv: jail=0 → passthrough; jail=1 → raises BrowserLauncherRequired
    (Finding B invariant: no bare-argv fallback when jail is active).
  - build_jail_env: credential/supervised env wiring (Finding 4).
  - push_egress_policy: socket absent → no raise (log warning).
  - push_egress_policy: mode selection default-deny vs open-logged.
  - Asymmetry teaching (open-logged) / autonomous (default-deny).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes.security.browser_jail import (
    BrowserLauncherRequired,
    _jail_enabled,
    build_jail_env,
    build_jailed_argv,
    push_egress_policy,
)

pytestmark = pytest.mark.unit

_BROWSER_ARGV = ["agent-browser", "--session", "exec-abc", "open", "https://example.com"]
_SESSION = "exec-abc"


class TestJailEnabled:
    def test_enabled_when_env_is_1(self) -> None:
        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "1"}):
            assert _jail_enabled() is True

    def test_disabled_when_env_is_0(self) -> None:
        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "0"}):
            assert _jail_enabled() is False

    def test_enabled_by_default_when_env_absent(self) -> None:
        import os  # noqa: PLC0415
        env = {k: v for k, v in os.environ.items() if k != "HERMES_BROWSER_JAIL"}
        with patch.dict("os.environ", env, clear=True):
            assert _jail_enabled() is True


class TestBuildJailedArgvJailOff:
    """CI path: jail=0 → browser_argv returned unchanged."""

    def test_returns_browser_argv_unchanged(self) -> None:
        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "0"}):
            result = build_jailed_argv(
                session_name=_SESSION,
                browser_argv=_BROWSER_ARGV,
            )
        assert result == _BROWSER_ARGV

    def test_passthrough_with_domains_whitelist(self) -> None:
        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "0"}):
            result = build_jailed_argv(
                session_name=_SESSION,
                browser_argv=_BROWSER_ARGV,
                domains_whitelist=("example.com",),
            )
        assert result == _BROWSER_ARGV


class TestBuildJailedArgvJailOn:
    """Finding B invariant: jail=1 → BrowserLauncherRequired (no bare-argv fallback)."""

    def test_raises_browser_launcher_required(self) -> None:
        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "1"}):
            with pytest.raises(BrowserLauncherRequired):
                build_jailed_argv(
                    session_name=_SESSION,
                    browser_argv=_BROWSER_ARGV,
                )

    def test_raises_for_any_session_name(self) -> None:
        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "1"}):
            with pytest.raises(BrowserLauncherRequired):
                build_jailed_argv(
                    session_name="exec-xyz999",
                    browser_argv=["chromium", "--headless"],
                )

    def test_raises_even_with_empty_whitelist(self) -> None:
        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "1"}):
            with pytest.raises(BrowserLauncherRequired):
                build_jailed_argv(
                    session_name=_SESSION,
                    browser_argv=_BROWSER_ARGV,
                    domains_whitelist=(),
                )

    def test_raises_even_with_non_empty_whitelist(self) -> None:
        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "1"}):
            with pytest.raises(BrowserLauncherRequired):
                build_jailed_argv(
                    session_name=_SESSION,
                    browser_argv=_BROWSER_ARGV,
                    domains_whitelist=("example.com",),
                )

    def test_error_message_mentions_launcher_client(self) -> None:
        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "1"}):
            with pytest.raises(BrowserLauncherRequired, match="BrowserLauncherClient"):
                build_jailed_argv(
                    session_name=_SESSION,
                    browser_argv=_BROWSER_ARGV,
                )

    def test_error_message_mentions_no_bare_argv_fallback(self) -> None:
        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "1"}):
            with pytest.raises(BrowserLauncherRequired, match="no bare-argv fallback"):
                build_jailed_argv(
                    session_name=_SESSION,
                    browser_argv=_BROWSER_ARGV,
                )


class TestBuildJailEnv:
    """Finding 4: credential/supervised env wiring for the jail script."""

    def test_has_credentials_sets_flag(self) -> None:
        env = build_jail_env(
            has_credentials=True, supervised=False, session_name=_SESSION
        )
        assert env["HERMES_JAIL_HAS_CREDENTIALS"] == "1"

    def test_no_credentials_clears_flag(self) -> None:
        env = build_jail_env(
            has_credentials=False, supervised=False, session_name=_SESSION
        )
        assert env["HERMES_JAIL_HAS_CREDENTIALS"] == "0"

    def test_supervised_sets_flag(self) -> None:
        env = build_jail_env(
            has_credentials=False, supervised=True, session_name=_SESSION
        )
        assert env["HERMES_JAIL_SUPERVISED"] == "1"

    def test_not_supervised_clears_flag(self) -> None:
        env = build_jail_env(
            has_credentials=False, supervised=False, session_name=_SESSION
        )
        assert env["HERMES_JAIL_SUPERVISED"] == "0"

    def test_session_name_in_env(self) -> None:
        env = build_jail_env(
            has_credentials=True, supervised=False, session_name=_SESSION
        )
        assert env["HERMES_BROWSER_SESSION"] == _SESSION

    def test_all_keys_present(self) -> None:
        env = build_jail_env(
            has_credentials=False, supervised=False, session_name=_SESSION
        )
        assert {"HERMES_JAIL_HAS_CREDENTIALS", "HERMES_JAIL_SUPERVISED",
                "HERMES_BROWSER_SESSION"}.issubset(env.keys())


class TestPushEgressPolicySocketMissing:
    def test_does_not_raise_when_socket_absent(self) -> None:
        push_egress_policy(
            session_name="test-session",
            domains_whitelist=(),
            teaching_mode=False,
        )

    def test_does_not_raise_with_whitelist_no_socket(self) -> None:
        push_egress_policy(
            session_name="test-session",
            domains_whitelist=("example.com", "cdn.example.com"),
            teaching_mode=False,
        )


class TestPushEgressPolicyMode:
    """Mode selection: open-logged vs default-deny."""

    def _capture_payload(
        self,
        domains_whitelist: tuple[str, ...],
        teaching_mode: bool,
    ) -> dict:
        captured: dict = {}

        def fake_send(sock_path: Path, data: bytes) -> None:
            captured["payload"] = json.loads(data.decode().strip())

        with patch("hermes.security.browser_jail._EGRESS_PROXY_SOCK") as mock_path, \
             patch("hermes.security.browser_jail._send_to_unix_sock", side_effect=fake_send):
            mock_path.exists.return_value = True
            push_egress_policy(
                session_name="s1",
                domains_whitelist=domains_whitelist,
                teaching_mode=teaching_mode,
            )

        return captured.get("payload", {})

    def test_no_whitelist_is_open_logged(self) -> None:
        payload = self._capture_payload(domains_whitelist=(), teaching_mode=False)
        assert payload.get("mode") == "open-logged"

    def test_whitelist_non_empty_is_default_deny(self) -> None:
        payload = self._capture_payload(
            domains_whitelist=("example.com",), teaching_mode=False
        )
        assert payload.get("mode") == "default-deny"

    def test_teaching_mode_is_always_open_logged(self) -> None:
        payload = self._capture_payload(
            domains_whitelist=("example.com",), teaching_mode=True
        )
        assert payload.get("mode") == "open-logged"

    def test_payload_contains_session_id(self) -> None:
        payload = self._capture_payload(domains_whitelist=(), teaching_mode=False)
        assert payload.get("session_id") == "s1"

    def test_payload_contains_domains_list(self) -> None:
        payload = self._capture_payload(
            domains_whitelist=("a.com", "b.com"), teaching_mode=False
        )
        assert set(payload.get("domains", [])) == {"a.com", "b.com"}
