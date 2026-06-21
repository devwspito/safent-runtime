"""Tests for the account-apply script validation and orchestration logic.

We test the pure validation functions and _apply() with subprocess mocked.
chpasswd/usermod/chage are NEVER called for real in tests.

The script lives at ops/agents-os-edition/scripts/hermes-account-apply and is
a standalone Python module. We import it by path using importlib so tests work
without it being installed as a package.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Import the script as a module via path (it is not a package).
# ---------------------------------------------------------------------------

_SCRIPT_PATH = (
    Path(__file__).parents[3]
    / "ops"
    / "agents-os-edition"
    / "scripts"
    / "hermes-account-apply"
)


def _load_script():
    # The script has no .py extension; we must supply the loader explicitly.
    loader = importlib.machinery.SourceFileLoader(
        "hermes_account_apply", str(_SCRIPT_PATH)
    )
    spec = importlib.util.spec_from_file_location(
        "hermes_account_apply", _SCRIPT_PATH, loader=loader
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_account_apply"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def script():
    return _load_script()


# ---------------------------------------------------------------------------
# Validation unit tests (pure)
# ---------------------------------------------------------------------------


class TestScriptUsernameValidation:
    def test_simple_accepted(self, script) -> None:
        assert script._validate_username("alice")

    def test_digit_start_rejected(self, script) -> None:
        assert not script._validate_username("1alice")

    def test_uppercase_rejected(self, script) -> None:
        assert not script._validate_username("Alice")

    def test_shell_injection_chars_rejected(self, script) -> None:
        for bad in (";ls", "$(id)", "`rm`", "../etc", "a b"):
            assert not script._validate_username(bad), f"should reject: {bad!r}"

    def test_hyphen_and_underscore_accepted(self, script) -> None:
        assert script._validate_username("hermes-user_01")

    def test_max_32_accepted(self, script) -> None:
        assert script._validate_username("a" * 32)

    def test_33_chars_rejected(self, script) -> None:
        assert not script._validate_username("a" * 33)


class TestScriptPasswordValidation:
    def test_8_chars_accepted(self, script) -> None:
        assert script._validate_password("12345678")

    def test_256_chars_accepted(self, script) -> None:
        assert script._validate_password("x" * 256)

    def test_7_chars_rejected(self, script) -> None:
        assert not script._validate_password("1234567")

    def test_257_chars_rejected(self, script) -> None:
        assert not script._validate_password("x" * 257)

    def test_newline_injection_rejected(self, script) -> None:
        # REGRESSION for CRITICAL: chpasswd reads stdin line-by-line.
        # Without this check a password "Aaaaaaa1\nroot:x" would inject a
        # second chpasswd entry, allowing root takeover via the privileged path.
        assert not script._validate_password("Aaaaaaa1\nroot:x")

    def test_carriage_return_rejected(self, script) -> None:
        assert not script._validate_password("Aaaaaaa1\r")

    def test_null_byte_rejected(self, script) -> None:
        assert not script._validate_password("Aaaaaaa1\x00")

    def test_tab_rejected(self, script) -> None:
        assert not script._validate_password("Aaaaaaa1\t")

    def test_del_char_rejected(self, script) -> None:
        assert not script._validate_password("Aaaaaaa1\x7f")

    def test_c0_control_chars_rejected(self, script) -> None:
        for code in (0x01, 0x07, 0x08, 0x1B, 0x1F):
            assert not script._validate_password("Aaaaaaa1" + chr(code)), (
                f"should reject C0 char 0x{code:02x}"
            )

    def test_space_accepted(self, script) -> None:
        assert script._validate_password("Aaaa aa1")

    def test_high_unicode_accepted(self, script) -> None:
        assert script._validate_password("Aaaaaaa1é")


# ---------------------------------------------------------------------------
# _apply() orchestration tests with subprocess mocked
# ---------------------------------------------------------------------------


def _make_ok_result():
    m = MagicMock()
    m.returncode = 0
    m.stderr = b""
    return m


def _make_fail_result(msg: str):
    m = MagicMock()
    m.returncode = 1
    m.stderr = msg.encode()
    return m


def _patch_subprocess(script, return_value):
    """Patch subprocess.run as seen by the hermes_account_apply module."""
    return patch.object(script.subprocess, "run", return_value=return_value)


class TestApplyHappyPath:
    def test_calls_chpasswd_via_stdin(self, script, tmp_path: Path) -> None:
        sentinel = tmp_path / "account-applied"
        with (
            patch.object(script, "SENTINEL_DIR", tmp_path),
            patch.object(script, "SENTINEL_FILE", sentinel),
            _patch_subprocess(script, _make_ok_result()) as mock_run,
        ):
            rc = script._apply({"username": "myuser", "password": "goodpassword"})

        assert rc == 0
        # First subprocess.run call must be chpasswd with input containing the password.
        chpasswd_call = mock_run.call_args_list[0]
        cmd = chpasswd_call.args[0]
        assert cmd[0].endswith("chpasswd")
        stdin_bytes = chpasswd_call.kwargs["input"]
        assert stdin_bytes == b"hermes-user:goodpassword"

    def test_calls_usermod_with_display_name(self, script, tmp_path: Path) -> None:
        sentinel = tmp_path / "account-applied"
        with (
            patch.object(script, "SENTINEL_DIR", tmp_path),
            patch.object(script, "SENTINEL_FILE", sentinel),
            _patch_subprocess(script, _make_ok_result()) as mock_run,
        ):
            script._apply({"username": "myuser", "password": "goodpassword"})

        # Second call is usermod -c
        usermod_call = mock_run.call_args_list[1]
        cmd = usermod_call.args[0]
        assert "/usr/sbin/usermod" in cmd[0]
        assert "-c" in cmd
        assert "myuser" in cmd

    def test_password_not_in_usermod_argv(self, script, tmp_path: Path) -> None:
        sentinel = tmp_path / "account-applied"
        with (
            patch.object(script, "SENTINEL_DIR", tmp_path),
            patch.object(script, "SENTINEL_FILE", sentinel),
            _patch_subprocess(script, _make_ok_result()) as mock_run,
        ):
            script._apply({"username": "myuser", "password": "secretpass99"})

        for call in mock_run.call_args_list:
            cmd_args = call.args[0]
            for arg in cmd_args:
                assert "secretpass99" not in str(arg), (
                    f"password must not appear in subprocess argv: {cmd_args}"
                )

    def test_sentinel_written_without_password(self, script, tmp_path: Path) -> None:
        sentinel = tmp_path / "account-applied"
        with (
            patch.object(script, "SENTINEL_DIR", tmp_path),
            patch.object(script, "SENTINEL_FILE", sentinel),
            _patch_subprocess(script, _make_ok_result()),
        ):
            script._apply({"username": "myuser", "password": "secretpass99"})

        assert sentinel.exists()
        content = json.loads(sentinel.read_text())
        assert "password" not in content
        assert content["display_name"] == "myuser"
        assert "applied_at" in content


class TestApplyValidationGate:
    def test_invalid_username_aborts_before_chpasswd(self, script) -> None:
        with _patch_subprocess(script, _make_ok_result()) as mock_run:
            rc = script._apply({"username": "1baduser", "password": "goodpassword"})
        assert rc == 1
        mock_run.assert_not_called()

    def test_short_password_aborts_before_chpasswd(self, script) -> None:
        with _patch_subprocess(script, _make_ok_result()) as mock_run:
            rc = script._apply({"username": "gooduser", "password": "short"})
        assert rc == 1
        mock_run.assert_not_called()

    def test_missing_username_key_aborts(self, script) -> None:
        with _patch_subprocess(script, _make_ok_result()) as mock_run:
            rc = script._apply({"password": "goodpassword"})
        assert rc == 1
        mock_run.assert_not_called()

    def test_newline_in_password_aborts_before_chpasswd(self, script) -> None:
        # REGRESSION for CRITICAL: the root-side gate must also reject control
        # chars even if the API endpoint was somehow bypassed.
        with _patch_subprocess(script, _make_ok_result()) as mock_run:
            rc = script._apply({"username": "gooduser", "password": "Aaaaaaa1\nroot:x"})
        assert rc == 1
        mock_run.assert_not_called()

    def test_null_byte_in_password_aborts_before_chpasswd(self, script) -> None:
        with _patch_subprocess(script, _make_ok_result()) as mock_run:
            rc = script._apply({"username": "gooduser", "password": "Aaaaaaa1\x00"})
        assert rc == 1
        mock_run.assert_not_called()


class TestApplyChpasswdFailure:
    def test_chpasswd_failure_returns_nonzero(self, script, tmp_path: Path) -> None:
        sentinel = tmp_path / "account-applied"
        fail = _make_fail_result("PAM error")
        with (
            patch.object(script, "SENTINEL_DIR", tmp_path),
            patch.object(script, "SENTINEL_FILE", sentinel),
            _patch_subprocess(script, fail),
        ):
            rc = script._apply({"username": "myuser", "password": "goodpassword"})
        assert rc == 1


# ---------------------------------------------------------------------------
# main() sentinel gate tests
# ---------------------------------------------------------------------------


class TestMainSentinelGate:
    def test_main_with_sentinel_present_returns_zero_without_subprocess(
        self, script, tmp_path: Path
    ) -> None:
        """Once the sentinel exists, main() must return 0 immediately without
        calling any subprocess — even if a staged file is present."""
        sentinel = tmp_path / "account-applied"
        sentinel.write_text("{}")
        stage_file = tmp_path / "account-request.json"
        stage_file.write_text('{"username":"u","password":"goodpass1"}')

        with (
            patch.object(script, "SENTINEL_FILE", sentinel),
            patch.object(script, "STAGE_FILE", stage_file),
            patch.object(script.os, "geteuid", return_value=0),
            _patch_subprocess(script, _make_ok_result()) as mock_run,
        ):
            rc = script.main()

        assert rc == 0
        mock_run.assert_not_called()

    def test_main_without_sentinel_proceeds(
        self, script, tmp_path: Path
    ) -> None:
        """Without a sentinel, main() proceeds past the gate.
        With no staged file present it exits cleanly (nothing to do)."""
        sentinel = tmp_path / "account-applied"
        stage_file = tmp_path / "account-request.json"

        with (
            patch.object(script, "SENTINEL_FILE", sentinel),
            patch.object(script, "STAGE_FILE", stage_file),
            patch.object(script.os, "geteuid", return_value=0),
        ):
            # No staged file — should exit cleanly with rc=0.
            rc = script.main()

        assert rc == 0


# ---------------------------------------------------------------------------
# _read_staged TOCTOU guard tests
# ---------------------------------------------------------------------------


class TestReadStagedToctouGuards:
    """Tests for the file integrity checks in _read_staged().

    In the test environment, files are owned by the current user (not 'hermes').
    We patch _hermes_uid() to return the current uid so the uid check passes
    when we want to test other guards in isolation, and to return a foreign uid
    when we want the uid check itself to fire.
    """

    def test_rejects_symlink(self, script, tmp_path: Path) -> None:
        """O_NOFOLLOW must cause _read_staged to raise when path is a symlink."""
        real_file = tmp_path / "real.json"
        real_file.write_text('{"username":"u","password":"goodpass1"}')
        symlink = tmp_path / "account-request.json"
        symlink.symlink_to(real_file)

        # On Linux, open() with O_NOFOLLOW on a symlink raises OSError (ELOOP).
        with (
            patch.object(script, "STAGE_FILE", symlink),
            pytest.raises(ValueError, match="cannot open staged file"),
        ):
            script._read_staged()

    def test_rejects_wrong_mode(self, script, tmp_path: Path) -> None:
        """A file with mode != 0o600 must be refused."""
        import os as _os

        stage_file = tmp_path / "account-request.json"
        stage_file.write_text('{"username":"u","password":"goodpass1"}')
        _os.chmod(stage_file, 0o644)  # group-readable — wrong

        current_uid = _os.getuid()
        with (
            patch.object(script, "STAGE_FILE", stage_file),
            patch("hermes_account_apply._hermes_uid", return_value=current_uid),
            pytest.raises(ValueError, match="0o600"),
        ):
            script._read_staged()

    def test_rejects_wrong_uid(self, script, tmp_path: Path) -> None:
        """A file owned by a uid != hermes uid must be refused."""
        import os as _os

        stage_file = tmp_path / "account-request.json"
        stage_file.write_text('{"username":"u","password":"goodpass1"}')
        _os.chmod(stage_file, 0o600)

        # Tell the script that 'hermes' uid is something other than our uid.
        current_uid = _os.getuid()
        with (
            patch.object(script, "STAGE_FILE", stage_file),
            patch(
                "hermes_account_apply._hermes_uid",
                return_value=current_uid + 9999,
            ),
            pytest.raises(ValueError, match="uid"),
        ):
            script._read_staged()
