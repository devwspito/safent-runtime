"""Regression tests — `_apply_runtime_landlock` fail-closed rewrite (spec 025
hallazgo #4: "Landlock fail-open").

Before the fix: `landlock_loader.load_and_apply()` returned exit code 0 for
BOTH "applied" and "skipped/degraded" (kernel without Landlock support,
unsupported architecture, seccomp blocking landlock_*, or a hard syscall
error) — `runtime/__main__.py:_apply_runtime_landlock` could not tell them
apart, so it only ever logged a warning and unconditionally continued,
letting the daemon build every agent plane (consent manager, tools registry,
broker…) with kernel confinement silently missing.

After the fix: `_apply_runtime_landlock` reads the TYPED `LandlockOutcome`
from `apply_runtime_landlock()` (+ the pre-existing `/boot` self-test, which
also catches "outcome=APPLIED but not actually enforcing" — the red-team
"written but not loaded" pattern) and refuses to start
(`sys.exit(1)`) unless Landlock is genuinely applied AND enforcing —
UNLESS the owner has set `HERMES_RUNTIME_LANDLOCK_ALLOW_DEGRADE=1`, in which
case it logs a loud, owner-visible warning and continues. The pre-existing
`HERMES_RUNTIME_LANDLOCK=0` full-disable knob is untouched.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hermes.runtime.__main__ import _apply_runtime_landlock
from hermes.security.landlock_loader import LandlockApplyResult, LandlockOutcome

pytestmark = pytest.mark.security


def _result(outcome: LandlockOutcome, exit_code: int = 0, detail: str = "x") -> LandlockApplyResult:
    return LandlockApplyResult(outcome=outcome, exit_code=exit_code, detail=detail)


@pytest.fixture(autouse=True)
def _clean_landlock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test should be sensitive to the real host's env — start from a clean slate."""
    monkeypatch.delenv("HERMES_RUNTIME_LANDLOCK", raising=False)
    monkeypatch.delenv("HERMES_RUNTIME_LANDLOCK_ALLOW_DEGRADE", raising=False)


class TestFailClosedByDefault:
    """No HERMES_RUNTIME_LANDLOCK_ALLOW_DEGRADE set → any non-enforcing outcome aborts boot."""

    def test_applied_and_enforcing_does_not_abort(self) -> None:
        with (
            patch(
                "hermes.security.landlock_loader.apply_runtime_landlock",
                return_value=_result(LandlockOutcome.APPLIED),
            ),
            patch("os.listdir", side_effect=PermissionError),  # /boot denied = enforcing
        ):
            _apply_runtime_landlock()  # must not raise

    def test_unsupported_kernel_refuses_to_start(self) -> None:
        with (
            patch(
                "hermes.security.landlock_loader.apply_runtime_landlock",
                return_value=_result(LandlockOutcome.UNSUPPORTED_KERNEL),
            ),
            patch("os.listdir", return_value=[]),
            pytest.raises(SystemExit) as exc_info,
        ):
            _apply_runtime_landlock()
        assert exc_info.value.code == 1

    def test_unsupported_arch_refuses_to_start(self) -> None:
        with (
            patch(
                "hermes.security.landlock_loader.apply_runtime_landlock",
                return_value=_result(LandlockOutcome.UNSUPPORTED_ARCH),
            ),
            patch("os.listdir", return_value=[]),
            pytest.raises(SystemExit) as exc_info,
        ):
            _apply_runtime_landlock()
        assert exc_info.value.code == 1

    def test_blocked_by_seccomp_refuses_to_start(self) -> None:
        with (
            patch(
                "hermes.security.landlock_loader.apply_runtime_landlock",
                return_value=_result(LandlockOutcome.BLOCKED, exit_code=2),
            ),
            patch("os.listdir", return_value=[]),
            pytest.raises(SystemExit) as exc_info,
        ):
            _apply_runtime_landlock()
        assert exc_info.value.code == 1

    def test_hard_syscall_error_refuses_to_start(self) -> None:
        with (
            patch(
                "hermes.security.landlock_loader.apply_runtime_landlock",
                return_value=_result(LandlockOutcome.ERROR, exit_code=3),
            ),
            patch("os.listdir", return_value=[]),
            pytest.raises(SystemExit) as exc_info,
        ):
            _apply_runtime_landlock()
        assert exc_info.value.code == 1

    def test_loader_crash_refuses_to_start(self) -> None:
        """An unexpected bug in the loader must NOT be treated as success."""
        with (
            patch(
                "hermes.security.landlock_loader.apply_runtime_landlock",
                side_effect=RuntimeError("bug in loader"),
            ),
            patch("os.listdir", return_value=[]),
            pytest.raises(SystemExit) as exc_info,
        ):
            _apply_runtime_landlock()
        assert exc_info.value.code == 1

    def test_applied_but_boot_still_readable_is_theater_refuses_to_start(self) -> None:
        """outcome=APPLIED but the /boot self-test proves it is NOT enforcing must
        ALSO refuse to start — exactly the red-team 'written but not loaded' gap
        the self-test exists to catch. Trusting the outcome alone would reintroduce
        the fail-open bug in a different shape."""
        with (
            patch(
                "hermes.security.landlock_loader.apply_runtime_landlock",
                return_value=_result(LandlockOutcome.APPLIED),
            ),
            patch("os.listdir", return_value=["vmlinuz"]),  # no exception = /boot readable
            pytest.raises(SystemExit) as exc_info,
        ):
            _apply_runtime_landlock()
        assert exc_info.value.code == 1


class TestExplicitDegradeFlag:
    """HERMES_RUNTIME_LANDLOCK_ALLOW_DEGRADE=1 — an explicit, logged, owner choice."""

    def test_allow_degrade_flag_permits_start_when_unsupported_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HERMES_RUNTIME_LANDLOCK_ALLOW_DEGRADE", "1")
        with (
            patch(
                "hermes.security.landlock_loader.apply_runtime_landlock",
                return_value=_result(LandlockOutcome.UNSUPPORTED_KERNEL),
            ),
            patch("os.listdir", return_value=[]),
        ):
            _apply_runtime_landlock()  # must not raise

    def test_allow_degrade_flag_logs_an_owner_visible_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("HERMES_RUNTIME_LANDLOCK_ALLOW_DEGRADE", "1")
        with (
            patch(
                "hermes.security.landlock_loader.apply_runtime_landlock",
                return_value=_result(LandlockOutcome.UNSUPPORTED_KERNEL),
            ),
            patch("os.listdir", return_value=[]),
            caplog.at_level("WARNING"),
        ):
            _apply_runtime_landlock()
        assert any(
            "runtime_landlock.degraded_ALLOWED" in record.message for record in caplog.records
        )

    def test_refusal_is_logged_before_exit(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch(
                "hermes.security.landlock_loader.apply_runtime_landlock",
                return_value=_result(LandlockOutcome.UNSUPPORTED_KERNEL),
            ),
            patch("os.listdir", return_value=[]),
            caplog.at_level("ERROR"),
            pytest.raises(SystemExit),
        ):
            _apply_runtime_landlock()
        assert any(
            "runtime_landlock.degraded_REFUSED" in record.message for record in caplog.records
        )


class TestExistingDisableFlagUnchanged:
    """HERMES_RUNTIME_LANDLOCK=0 — the pre-existing full-disable knob, untouched."""

    def test_hermes_runtime_landlock_0_still_skips_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HERMES_RUNTIME_LANDLOCK", "0")
        with patch("hermes.security.landlock_loader.apply_runtime_landlock") as mock_apply:
            _apply_runtime_landlock()  # must not raise
        mock_apply.assert_not_called()
