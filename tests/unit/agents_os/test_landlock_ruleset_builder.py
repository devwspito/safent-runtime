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


class TestRuntimeCapabilityGrantsRefer:
    """MCP-04 root cause (spec 025 matriz, live-verified): without
    AccessRight.REFER on /var/lib/hermes, the kernel denies EVERY rename()/
    link() whose destination has a DIFFERENT parent directory than the
    source — even two directories on the same filesystem, covered by this
    exact same rule — with EXDEV (errno 18). `uv`'s cache-population rename
    (`uv-cache/.tmpXXXX` -> `uv-cache/archive-v0/<hash>`) is exactly that
    shape; every uncached Python MCP install hit it. (The OTHER half of the
    fix — the ABI-mask table that was silently stripping `refer` back out
    on any kernel newer than ABI 3 — is pinned in
    test_landlock_loader.py::TestMaxAccessFsMaskBeyondTheKnownAbiTable.)"""

    def test_var_lib_hermes_rule_grants_refer(self) -> None:
        spec = LandlockRulesetBuilder().build(Capability.RUNTIME)
        rule = next(r for r in spec.rules if r.path == "/var/lib/hermes")
        assert AccessRight.REFER in rule.accesses

    def test_refer_is_in_the_ruleset_handled_mask(self) -> None:
        spec = LandlockRulesetBuilder().build(Capability.RUNTIME)
        assert AccessRight.REFER in spec.handled_access_fs

    def test_the_broad_read_only_var_rule_does_not_need_refer(self) -> None:
        """REFER only matters for a rule that can already create/remove —
        the read-only `/var` rule is unaffected and must stay read-only."""
        spec = LandlockRulesetBuilder().build(Capability.RUNTIME)
        rule = next(r for r in spec.rules if r.path == "/var")
        assert AccessRight.WRITE_FILE not in rule.accesses

    def test_refer_does_not_reach_paths_that_never_needed_it(self) -> None:
        """Least privilege (security review 2026-09-10 MEDIUM follow-up):
        REFER is granted ONLY on /var/lib/hermes — the one live-verified
        need (MCP-04) — not by blanket symmetry across every RW path."""
        spec = LandlockRulesetBuilder().build(Capability.RUNTIME)
        for rule in spec.rules:
            if rule.path == "/var/lib/hermes":
                continue
            assert AccessRight.REFER not in rule.accesses, rule.path


class TestRefIsRuntimeOnlyNotBrowserController:
    """Security review 2026-09-10 (MEDIUM finding, CWE-732): REFER used to
    live IN the shared `_RUNTIME_RW` frozenset, so `Capability.
    BROWSER_CONTROLLER` — a deliberately tighter profile that reuses that
    same set on /run, /tmp, /dev, browser-sessions, and /var/lib/hermes/tmp
    specifically to deny master.key (see that capability's own comment) —
    silently gained it too. Before this fix every cross-directory rename in
    that ruleset was unconditionally denied; this pins that RUNTIME keeps
    REFER (the MCP-04 fix) while BROWSER_CONTROLLER does not regain it."""

    def test_runtime_handled_mask_has_refer(self) -> None:
        spec = LandlockRulesetBuilder().build(Capability.RUNTIME)
        assert AccessRight.REFER in spec.handled_access_fs

    def test_browser_controller_handled_mask_does_not_have_refer(self) -> None:
        spec = LandlockRulesetBuilder().build(Capability.BROWSER_CONTROLLER)
        assert AccessRight.REFER not in spec.handled_access_fs

    def test_no_browser_controller_rule_grants_refer(self) -> None:
        spec = LandlockRulesetBuilder().build(Capability.BROWSER_CONTROLLER)
        for rule in spec.rules:
            assert AccessRight.REFER not in rule.accesses, rule.path

    def test_browser_controller_keeps_every_other_right_unchanged(self) -> None:
        """The fix must not narrow anything the controller already had —
        only stop it from gaining REFER. Every rule keeps the exact same
        non-REFER rights it had before this fix (WRITE_FILE/MAKE_REG/
        REMOVE_FILE/etc — see _RUNTIME_RW)."""
        from hermes.agents_os.infrastructure.landlock_ruleset_builder import _RUNTIME_RW

        spec = LandlockRulesetBuilder().build(Capability.BROWSER_CONTROLLER)
        rw_paths = {"/run", "/tmp", "/dev", "/var/lib/hermes/browser-sessions", "/var/lib/hermes/tmp"}
        for rule in spec.rules:
            if rule.path in rw_paths:
                assert rule.accesses == _RUNTIME_RW, rule.path

    @pytest.mark.parametrize(
        "capability",
        [c for c in Capability if c not in (Capability.RUNTIME, Capability.BROWSER_CONTROLLER)],
    )
    def test_no_other_capability_was_touched_by_this_fix(self, capability: Capability) -> None:
        """Audits every OTHER capability too (matching the review's own
        diligence) — REFER must appear nowhere outside RUNTIME."""
        try:
            spec = LandlockRulesetBuilder().build(capability)
        except ValueError:
            pytest.skip(f"{capability} needs template params (e.g. BROWSER session)")
        assert AccessRight.REFER not in spec.handled_access_fs, capability


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


class TestBrowserControllerConfinement:
    """BROWSER_CONTROLLER (2026-07-05 audit): the agent-browser CDP controller shim
    applies this ruleset so the controller loses READ of the daemon's keystore
    while keeping what a --cdp attach needs. Landlock stacks with the daemon's
    RUNTIME ruleset (effective = intersection), so OMITTING /var here denies
    master.key."""

    def _paths(self) -> list[str]:
        spec = LandlockRulesetBuilder().build(Capability.BROWSER_CONTROLLER)
        assert spec.capability == Capability.BROWSER_CONTROLLER
        return [r.path for r in spec.rules]

    def test_denies_the_keystore(self) -> None:
        paths = self._paths()
        # The two grants that would leak master.key/keys/shell-state.db MUST be absent.
        assert "/var" not in paths, "/var RX would grant READ of master.key"
        assert "/var/lib/hermes" not in paths, "/var/lib/hermes RW would grant master.key"
        # No granted path may be a prefix of the keystore files.
        keystore = "/var/lib/hermes/master.key"
        for p in paths:
            assert not keystore.startswith(p.rstrip("/") + "/") and p != keystore, (
                f"path {p!r} would grant access to the keystore"
            )

    def test_grants_what_the_controller_needs(self) -> None:
        paths = self._paths()
        # libs/node/binary, certs, /proc, and a writable socket dir (/tmp) + /dev.
        for needed in ("/usr", "/etc", "/lib", "/proc", "/tmp", "/dev", "/run"):
            assert needed in paths, f"controller needs {needed}"
        # Its own session subtree is granted narrowly (NOT the keystore root).
        assert "/var/lib/hermes/browser-sessions" in paths
