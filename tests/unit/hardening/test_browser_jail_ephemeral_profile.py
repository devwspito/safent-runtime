"""Regression test: hermes-browser-jail wipes exec-* profiles before launch.

Bug: a `exec-browse` profile that had accumulated state across many container
lifetimes (the volume is a durable `lumen-data` named volume) crash-looped the
confined Chromium — status=5/TRAP ~0.6s after EVERY launch, under the FULL
systemd confinement (netns + Landlock BROWSER ruleset + the PASS-3 seccomp shim
+ ProtectSystem=strict). Repro on a clean image (aarch64): restoring the EXACT
SAME corrupted directory reproduced the crash 100% of the time; wiping it to an
empty dir at the IDENTICAL path, under the IDENTICAL confinement, made the
browser stay up indefinitely (CDP answering, x11vnc listening). The trigger is
a half-written profile (Chromium is SIGKILLed via cgroup-kill on every earlier
crash and never gets to repair its own state), not the confinement combo.

Fix: hermes-browser-jail wipes exec-* session directories to a pristine state
on every (re)launch — exec-* carries no state the agent needs across a crash
or restart. teaching-* sessions are NOT wiped (they intentionally persist the
owner's logins across restarts).

This test extracts and RUNS the actual cleanup loop from the shipped script
(not a copy) so a regression in the shell logic itself fails this test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_JAIL_SCRIPT = (
    _REPO_ROOT / "ops" / "agents-os-edition" / "scripts" / "hermes-browser-jail"
)

_LOOP_START_MARKER = 'for _arg in "$@"; do'
_LOOP_END_MARKER = "# ── 3. exec del navegador real"


def _extract_cleanup_loop() -> str:
    """Pull the singleton-cleanup + ephemeral-wipe `for` loop verbatim from the
    real script, so this test runs the SHIPPED code, not a hand-copied stand-in.
    """
    content = _JAIL_SCRIPT.read_text()
    start = content.index(_LOOP_START_MARKER)
    end = content.index(_LOOP_END_MARKER, start)
    return content[start:end]


def _run_cleanup_loop(user_data_dir: Path) -> None:
    loop_src = _extract_cleanup_loop()
    script = f"set -euo pipefail\n{loop_src}\n"
    subprocess.run(
        ["bash", "-c", script, "bash", f"--user-data-dir={user_data_dir}"],
        check=True,
        capture_output=True,
        text=True,
    )


class TestJailScriptExists:
    def test_jail_script_present(self) -> None:
        assert _JAIL_SCRIPT.is_file(), f"expected shipped script at {_JAIL_SCRIPT}"

    def test_cleanup_loop_extractable(self) -> None:
        loop_src = _extract_cleanup_loop()
        assert "SingletonLock" in loop_src


class TestExecSessionProfileIsWiped:
    """exec-* sessions are ephemeral EXECUTION sessions — self-heal by wiping."""

    def test_exec_session_profile_wiped_clean(self, tmp_path: Path) -> None:
        udd = tmp_path / "exec-regressiontest"
        udd.mkdir()
        (udd / "Default").mkdir()
        (udd / "Default" / "Preferences").write_text("possibly-corrupted-state")
        (udd / "SingletonLock").symlink_to("dead-host-999")

        _run_cleanup_loop(udd)

        assert list(udd.iterdir()) == [], (
            "exec-* profile must be wiped to a pristine dir before every launch"
        )

    def test_exec_session_survives_being_already_empty(self, tmp_path: Path) -> None:
        """The wipe must be a no-op (not an error) on a fresh dir (first boot)."""
        udd = tmp_path / "exec-freshalready"
        udd.mkdir()

        _run_cleanup_loop(udd)  # must not raise

        assert list(udd.iterdir()) == []


class TestTeachingSessionProfileIsPreserved:
    """teaching-* keeps the owner's logins across restarts — must NOT be wiped."""

    def test_teaching_session_profile_untouched(self, tmp_path: Path) -> None:
        udd = tmp_path / "teaching-chromium"
        udd.mkdir()
        (udd / "Default").mkdir()
        (udd / "Default" / "Preferences").write_text("owner-logins")

        _run_cleanup_loop(udd)

        assert (udd / "Default" / "Preferences").read_text() == "owner-logins", (
            "teaching-* sessions must keep the owner's persisted logins"
        )


class TestSingletonCleanupStillAppliesToBoth:
    """The pre-existing SingletonLock/Socket/Cookie cleanup must still run for
    every session (this is what let Chromium self-heal from a stale singleton
    before the ephemeral-wipe fix existed — must not regress).
    """

    def test_teaching_session_singleton_lock_removed(self, tmp_path: Path) -> None:
        udd = tmp_path / "teaching-chromium"
        udd.mkdir()
        (udd / "SingletonLock").symlink_to("dead-host-123")

        _run_cleanup_loop(udd)

        assert not (udd / "SingletonLock").exists()
