"""Tests LandlockRulesetBuilder (FR-052 BLOQUEANTE)."""

from __future__ import annotations

import pytest

from hermes.agents_os.application.consent_manager import Capability
from hermes.agents_os.infrastructure.landlock_ruleset_builder import (
    AccessRight,
    LandlockRulesetBuilder,
    build_browser_ruleset,
    serialize_for_audit,
)

pytestmark = pytest.mark.unit


class TestBuild:
    def test_documents_capability_grants_only_documents_dir(self) -> None:
        spec = LandlockRulesetBuilder().build(Capability.DOCUMENTS)
        assert spec.capability == Capability.DOCUMENTS
        paths = [r.path for r in spec.rules]
        assert paths == ["/home/hermes/Documents"]
        assert AccessRight.WRITE_FILE in spec.handled_access_fs
        assert AccessRight.MAKE_REG in spec.handled_access_fs

    def test_terminal_includes_workspace_and_binaries(self) -> None:
        spec = LandlockRulesetBuilder().build(Capability.TERMINAL)
        paths = [r.path for r in spec.rules]
        assert "/usr/bin/bash" in paths
        assert "/var/lib/hermes/terminal-workspace" in paths

    def test_camera_has_no_fs_rules(self) -> None:
        spec = LandlockRulesetBuilder().build(Capability.CAMERA)
        assert spec.rules == ()
        # Aún así sigue siendo deny_all_network y handled_access_fs vacío.
        assert spec.handled_access_fs == frozenset()

    def test_filesystem_full_warns_implicitly_by_path_width(self) -> None:
        spec = LandlockRulesetBuilder().build(Capability.FILESYSTEM_FULL)
        assert spec.rules[0].path == "/home/hermes"
        assert AccessRight.REMOVE_FILE in spec.handled_access_fs

    def test_user_home_override(self) -> None:
        builder = LandlockRulesetBuilder(user_home_user="alice")
        spec = builder.build(Capability.DOCUMENTS)
        assert spec.rules[0].path == "/home/alice/Documents"


class TestAggregated:
    def test_multiple_caps_sorted(self) -> None:
        specs = LandlockRulesetBuilder().build_aggregated(
            frozenset({Capability.TERMINAL, Capability.DOCUMENTS})
        )
        caps = [s.capability for s in specs]
        assert caps == [Capability.DOCUMENTS, Capability.TERMINAL]

    def test_empty_aggregation(self) -> None:
        specs = LandlockRulesetBuilder().build_aggregated(frozenset())
        assert specs == ()


class TestBrowserCapability:
    """spec 009 §4 — Capability.BROWSER minimal ruleset."""

    def test_browser_ruleset_contains_browser_exec_binary(self) -> None:
        # El navegador se lanza vía un shim Landlock self-apply:
        # `/usr/bin/python3 -c <shim> /usr/bin/chromium-browser …` (chromium vive
        # bajo /ms-playwright). El binario ejecutable del ruleset es python3, no
        # un /usr/bin/agent-browser directo.
        spec = build_browser_ruleset("my-session")
        paths = [r.path for r in spec.rules]
        assert "/usr/bin/python3" in paths
        assert "/ms-playwright" in paths

    def test_browser_ruleset_session_path_resolved(self) -> None:
        spec = build_browser_ruleset("my-session")
        paths = [r.path for r in spec.rules]
        assert "/var/lib/hermes/browser-sessions/my-session" in paths

    def test_browser_session_dir_has_write_access(self) -> None:
        spec = build_browser_ruleset("sess-abc")
        session_rule = next(
            r for r in spec.rules
            if r.path == "/var/lib/hermes/browser-sessions/sess-abc"
        )
        assert AccessRight.WRITE_FILE in session_rule.accesses
        assert AccessRight.MAKE_REG in session_rule.accesses
        assert AccessRight.TRUNCATE in session_rule.accesses

    def test_browser_binary_is_read_exec_only(self) -> None:
        spec = build_browser_ruleset("s1")
        binary_rule = next(
            r for r in spec.rules if r.path == "/usr/bin/python3"
        )
        assert AccessRight.READ_FILE in binary_rule.accesses
        assert AccessRight.EXECUTE in binary_rule.accesses
        # No write on the binary path.
        assert AccessRight.WRITE_FILE not in binary_rule.accesses

    def test_browser_ruleset_no_hermes_config_or_run_path(self) -> None:
        spec = build_browser_ruleset("s1")
        paths = [r.path for r in spec.rules]
        # /etc/hermes and /run/hermes must NOT appear — Landlock denies by default.
        assert not any("/etc/hermes" in p for p in paths)
        assert not any("/run/hermes" in p for p in paths)

    def test_browser_ruleset_no_other_sessions(self) -> None:
        spec = build_browser_ruleset("sess-A")
        paths = [r.path for r in spec.rules]
        # Only the current session is whitelisted — other sessions denied.
        assert all(
            "browser-sessions/sess-B" not in p for p in paths
        )

    def test_browser_ruleset_capability_field(self) -> None:
        spec = build_browser_ruleset("x")
        assert spec.capability == Capability.BROWSER

    def test_browser_ruleset_deny_all_network_informative(self) -> None:
        # deny_all_network is True (informative — actual network is netns+nft).
        spec = build_browser_ruleset("x")
        assert spec.deny_all_network is True

    def test_build_browser_ruleset_via_builder_session_name(self) -> None:
        builder = LandlockRulesetBuilder(session_name="sess-xyz")
        spec = builder.build(Capability.BROWSER)
        paths = [r.path for r in spec.rules]
        assert "/var/lib/hermes/browser-sessions/sess-xyz" in paths

    def test_browser_ruleset_fonts_and_certs(self) -> None:
        spec = build_browser_ruleset("s")
        paths = [r.path for r in spec.rules]
        assert "/usr/share/fonts" in paths
        assert "/etc/ssl/certs" in paths


class TestSerialize:
    def test_serialize_contains_capability_and_paths(self) -> None:
        spec = LandlockRulesetBuilder().build(Capability.DOWNLOADS)
        payload = serialize_for_audit(spec)
        assert payload["capability"] == "downloads"
        assert payload["rules"][0]["path"] == "/home/hermes/Downloads"
        assert "write_file" in payload["rules"][0]["accesses"]
        # Ordenado para audit reproducible.
        assert payload["handled_access_fs"] == sorted(
            payload["handled_access_fs"]
        )

    def test_serialize_deny_all_network_default_true(self) -> None:
        spec = LandlockRulesetBuilder().build(Capability.DOCUMENTS)
        assert serialize_for_audit(spec)["deny_all_network"] is True
