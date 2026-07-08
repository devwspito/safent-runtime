"""Tests for the hermes-remote-access-control root helper script.

Mirrors the pattern of test_account_apply.py: import the script via path,
mock subprocess / PAM, never call real systemctl or PAM.

Coverage:
  - _read_staged: TOCTOU checks (uid, mode, nlink).
  - _apply: disable with correct password → systemctl disable --now called.
  - _apply: disable with wrong password (PAM returns False) → systemctl NOT called.
  - _apply: enable → systemctl enable --now called (no PAM).
  - _apply: unknown action → systemctl NOT called, returns 1.
  - _apply: disable with missing password field → systemctl NOT called.
  - main: shreds the staged file even on failure.
  - _verify_password_pam: falls back to unix_chkpwd when pam not importable.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Load the script as a module (no .py extension).
# ---------------------------------------------------------------------------

_SCRIPT_PATH = (
    Path(__file__).parents[3]
    / "ops"
    / "agents-os-edition"
    / "scripts"
    / "hermes-remote-access-control"
)


def _load_script():
    loader = importlib.machinery.SourceFileLoader(
        "hermes_remote_access_control", str(_SCRIPT_PATH)
    )
    spec = importlib.util.spec_from_file_location(
        "hermes_remote_access_control", _SCRIPT_PATH, loader=loader
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_remote_access_control"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_staged_file(tmp_path: Path, payload: dict) -> Path:
    stage_dir = tmp_path / "remote-control"
    stage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    f = stage_dir / "request.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(f, 0o600)
    return f


# ---------------------------------------------------------------------------
# _read_staged TOCTOU checks
# ---------------------------------------------------------------------------


class TestReadStagedValidation:
    def test_refuses_wrong_uid(self, tmp_path: Path) -> None:
        stage_dir = tmp_path / "remote-control"
        stage_dir.mkdir(mode=0o700, parents=True)
        f = stage_dir / "request.json"
        f.write_text('{"action":"enable"}')
        os.chmod(f, 0o600)

        # Simulate uid mismatch: the file is owned by the test runner's uid,
        # but we tell the script that the 'hermes' uid is something else.
        with patch.object(mod, "STAGE_FILE", f):
            with patch.object(mod, "_hermes_uid", return_value=99999):
                with pytest.raises(ValueError, match="uid"):
                    mod._read_staged()

    def test_refuses_wrong_mode(self, tmp_path: Path) -> None:
        stage_dir = tmp_path / "remote-control"
        stage_dir.mkdir(mode=0o700, parents=True)
        f = stage_dir / "request.json"
        f.write_text('{"action":"enable"}')
        os.chmod(f, 0o644)  # wrong — should be 0600

        with patch.object(mod, "STAGE_FILE", f):
            with patch.object(mod, "_hermes_uid", return_value=os.getuid()):
                with pytest.raises(ValueError, match="mode"):
                    mod._read_staged()


# ---------------------------------------------------------------------------
# _apply: disable
# ---------------------------------------------------------------------------


class TestApplyDisable:
    def test_correct_password_calls_systemctl_disable(self) -> None:
        with (
            patch.object(mod, "_verify_password_pam", return_value=True),
            patch.object(mod, "_disable_remote_access", return_value=True) as mock_dis,
        ):
            rc = mod._apply({"action": "disable", "password": "correctpassword"})
        assert rc == 0
        mock_dis.assert_called_once()

    def test_wrong_password_does_not_call_systemctl(self) -> None:
        with (
            patch.object(mod, "_verify_password_pam", return_value=False),
            patch.object(mod, "_disable_remote_access") as mock_dis,
        ):
            rc = mod._apply({"action": "disable", "password": "wrongpassword"})
        assert rc == 1
        mock_dis.assert_not_called()

    def test_missing_password_field_rejected(self) -> None:
        with patch.object(mod, "_disable_remote_access") as mock_dis:
            rc = mod._apply({"action": "disable"})
        assert rc == 1
        mock_dis.assert_not_called()

    def test_empty_password_rejected(self) -> None:
        with patch.object(mod, "_disable_remote_access") as mock_dis:
            rc = mod._apply({"action": "disable", "password": ""})
        assert rc == 1
        mock_dis.assert_not_called()

    def test_systemctl_failure_returns_1(self) -> None:
        with (
            patch.object(mod, "_verify_password_pam", return_value=True),
            patch.object(mod, "_disable_remote_access", return_value=False),
        ):
            rc = mod._apply({"action": "disable", "password": "correctpassword"})
        assert rc == 1


# ---------------------------------------------------------------------------
# _apply: enable
# ---------------------------------------------------------------------------


class TestApplyEnable:
    # Security fix (finding #6): enable now requires the same PAM verification
    # as disable. Enabling remote access exposes the device to the internet; an
    # unauthenticated enable is as dangerous as an unauthenticated disable.

    def test_enable_with_valid_password_calls_systemctl_enable(self) -> None:
        """enable + PAM OK → _enable_remote_access called, rc=0."""
        with (
            patch.object(mod, "_verify_password_pam", return_value=True),
            patch.object(mod, "_enable_remote_access", return_value=True) as mock_en,
        ):
            rc = mod._apply({"action": "enable", "password": "correctpassword"})
        assert rc == 0
        mock_en.assert_called_once()

    def test_enable_without_password_rejected(self) -> None:
        """enable without a password field must be rejected before PAM or systemctl."""
        with (
            patch.object(mod, "_enable_remote_access") as mock_en,
            patch.object(mod, "_verify_password_pam") as mock_pam,
        ):
            rc = mod._apply({"action": "enable"})
        assert rc == 1
        mock_en.assert_not_called()
        mock_pam.assert_not_called()

    def test_enable_with_wrong_password_rejected(self) -> None:
        """enable with a bad password (PAM returns False) must not call systemctl."""
        with (
            patch.object(mod, "_verify_password_pam", return_value=False),
            patch.object(mod, "_enable_remote_access") as mock_en,
        ):
            rc = mod._apply({"action": "enable", "password": "wrongpassword"})
        assert rc == 1
        mock_en.assert_not_called()

    def test_enable_with_empty_password_rejected(self) -> None:
        """Empty string password must be rejected without calling PAM or systemctl."""
        with (
            patch.object(mod, "_enable_remote_access") as mock_en,
            patch.object(mod, "_verify_password_pam") as mock_pam,
        ):
            rc = mod._apply({"action": "enable", "password": ""})
        assert rc == 1
        mock_en.assert_not_called()
        mock_pam.assert_not_called()

    def test_systemctl_failure_returns_1(self) -> None:
        """PAM passes but systemctl reports failure → rc=1."""
        with (
            patch.object(mod, "_verify_password_pam", return_value=True),
            patch.object(mod, "_enable_remote_access", return_value=False),
        ):
            rc = mod._apply({"action": "enable", "password": "correctpassword"})
        assert rc == 1


# ---------------------------------------------------------------------------
# _apply: unknown action
# ---------------------------------------------------------------------------


class TestApplyUnknownAction:
    def test_unknown_action_returns_1_without_systemctl(self) -> None:
        with (
            patch.object(mod, "_enable_remote_access") as mock_en,
            patch.object(mod, "_disable_remote_access") as mock_dis,
        ):
            rc = mod._apply({"action": "reboot"})
        assert rc == 1
        mock_en.assert_not_called()
        mock_dis.assert_not_called()

    def test_empty_action_returns_1(self) -> None:
        rc = mod._apply({"action": ""})
        assert rc == 1

    def test_missing_action_returns_1(self) -> None:
        rc = mod._apply({})
        assert rc == 1


# ---------------------------------------------------------------------------
# _systemctl helper
# ---------------------------------------------------------------------------


class TestSystemctlHelper:
    def test_disable_now_calls_correct_args(self) -> None:
        # _disable_remote_access issues TWO systemctl calls: first
        # `disable --now <remote services>`, then `restart <shell service>` to
        # drop the compositor back to local render. Assert on the disable call
        # (not call_args, which is the trailing restart) and on the restart.
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            mod._disable_remote_access()

        all_args = [c[0][0] for c in mock_run.call_args_list]
        disable_args = next(a for a in all_args if "disable" in a)
        assert "--now" in disable_args
        # Single-origin gateway + noVNC are the fixed remote services; the tunnel
        # is selected at runtime (named vs quick), so assert its family loosely.
        assert "hermes-remote-gateway.service" in disable_args
        assert "hermes-novnc.service" in disable_args
        assert any("tunnel" in s for s in disable_args)
        # The disabled set must be exactly the module's remote services.
        for svc in mod._remote_services():
            assert svc in disable_args
        # Returning to local render restarts the shell/compositor service.
        assert any("restart" in a and mod._SHELL_SERVICE in a for a in all_args)

    def test_enable_now_calls_correct_args(self, tmp_path: Path) -> None:
        # _enable_remote_access first writes the VNC-mirror display flag and
        # restarts the shell, THEN `enable --now`s the remote services. Redirect
        # the flag paths into tmp_path so the non-root test can reach systemctl.
        mock_result = MagicMock()
        mock_result.returncode = 0
        display_env = tmp_path / "remote-display.env"
        active_file = tmp_path / "remote-active"
        with (
            patch("subprocess.run", return_value=mock_result) as mock_run,
            patch.object(mod, "_REMOTE_DISPLAY_ENV", display_env),
            patch.object(mod, "_REMOTE_ACTIVE_FILE", active_file),
        ):
            mod._enable_remote_access()

        all_args = [c[0][0] for c in mock_run.call_args_list]
        enable_args = next(a for a in all_args if "enable" in a)
        assert "--now" in enable_args
        assert "hermes-remote-gateway.service" in enable_args
        assert "hermes-novnc.service" in enable_args
        assert any("tunnel" in s for s in enable_args)
        for svc in mod._remote_services():
            assert svc in enable_args
        # VNC mirror mode: the shell/compositor is restarted and the flags written.
        assert any("restart" in a and mod._SHELL_SERVICE in a for a in all_args)
        assert display_env.read_text(encoding="utf-8") == mod._REMOTE_DISPLAY_CONTENT
        assert active_file.read_text(encoding="utf-8") == "1"


# ---------------------------------------------------------------------------
# main: shred on failure
# ---------------------------------------------------------------------------


class TestMainShred:
    def test_shreds_file_on_parse_failure(self, tmp_path: Path) -> None:
        stage_dir = tmp_path / "rc"
        stage_dir.mkdir()
        f = stage_dir / "request.json"
        f.write_text("INVALID JSON NOT A DICT")
        os.chmod(f, 0o600)

        with (
            patch.object(mod, "STAGE_FILE", f),
            patch.object(mod, "_hermes_uid", return_value=os.getuid()),
        ):
            mod.main.__wrapped__(f) if hasattr(mod.main, "__wrapped__") else None
            # Patch STAGE_FILE and run main as root=0
            with patch("os.geteuid", return_value=0):
                rc = mod.main()

        # File should be gone (shredded).
        assert not f.exists()
        assert rc == 1


# ---------------------------------------------------------------------------
# _verify_password_pam fallback path
# ---------------------------------------------------------------------------


class TestVerifyPasswordPam:
    def test_returns_false_on_error(self) -> None:
        with patch.dict(sys.modules, {"pam": None}):
            with patch.object(mod, "_verify_password_unix_chkpwd", return_value=False):
                result = mod._verify_password_pam("hermes-user", "wrongpassword")
        assert result is False

    def test_returns_true_via_unix_chkpwd_success(self) -> None:
        with patch.object(mod, "_account_has_real_password", return_value=True):
            with patch.dict(sys.modules, {"pam": None}):
                with patch.object(mod, "_verify_password_unix_chkpwd", return_value=True):
                    result = mod._verify_password_pam("hermes-user", "correctpassword")
        assert result is True

    def test_unix_chkpwd_returns_false_on_nonzero_rc(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("subprocess.run", return_value=mock_result):
            result = mod._verify_password_unix_chkpwd("hermes-user", "wrongpassword")
        assert result is False

    def test_unix_chkpwd_returns_true_on_zero_rc(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            result = mod._verify_password_unix_chkpwd("hermes-user", "correctpassword")
        assert result is True

    def test_unix_chkpwd_returns_false_on_subprocess_error(self) -> None:
        with patch("subprocess.run", side_effect=OSError("no binary")):
            result = mod._verify_password_unix_chkpwd("hermes-user", "any")
        assert result is False

    def test_pam_correct_password_returns_true(self) -> None:
        mock_pam_mod = MagicMock()
        mock_auth = MagicMock()
        mock_auth.authenticate.return_value = True
        mock_pam_mod.pam.return_value = mock_auth

        with patch.object(mod, "_account_has_real_password", return_value=True):
            with patch.dict(sys.modules, {"pam": mock_pam_mod}):
                result = mod._verify_password_pam("hermes-user", "correctpassword")
        assert result is True

    def test_pam_wrong_password_returns_false(self) -> None:
        mock_pam_mod = MagicMock()
        mock_auth = MagicMock()
        mock_auth.authenticate.return_value = False
        mock_pam_mod.pam.return_value = mock_auth

        with patch.dict(sys.modules, {"pam": mock_pam_mod}):
            result = mod._verify_password_pam("hermes-user", "wrongpassword")
        assert result is False


# ---------------------------------------------------------------------------
# Fail-closed: una cuenta sin contraseña real NUNCA pasa el gate (bug VM:
# passwordless autologin → PAM acepta cualquier cosa → fail-OPEN). Regresión.
# ---------------------------------------------------------------------------

from unittest.mock import mock_open  # noqa: E402


class TestAccountHasRealPassword:
    def test_locked_bang_fails_closed(self) -> None:
        with patch("builtins.open", mock_open(read_data="hermes-user:!:19000:0:99999:7:::\n")):
            assert mod._account_has_real_password("hermes-user") is False

    def test_locked_star_fails_closed(self) -> None:
        with patch("builtins.open", mock_open(read_data="hermes-user:*:19000:0:99999:7:::\n")):
            assert mod._account_has_real_password("hermes-user") is False

    def test_empty_hash_fails_closed(self) -> None:
        with patch("builtins.open", mock_open(read_data="hermes-user::19000:0:99999:7:::\n")):
            assert mod._account_has_real_password("hermes-user") is False

    def test_user_absent_fails_closed(self) -> None:
        with patch("builtins.open", mock_open(read_data="root:$6$x$y:19000::::::\n")):
            assert mod._account_has_real_password("hermes-user") is False

    def test_real_hash_passes(self) -> None:
        with patch("builtins.open", mock_open(read_data="hermes-user:$6$abc$def:19000:0:99999:7:::\n")):
            assert mod._account_has_real_password("hermes-user") is True


class TestVerifyFailsClosedWithoutPassword:
    def test_passwordless_account_rejected_even_with_nonempty_password(self) -> None:
        # El gate NUNCA debe pasar si la cuenta no tiene contraseña real, aunque
        # PAM aceptara cualquier cosa (fail-OPEN del estado pre-onboarding).
        with patch.object(mod, "_account_has_real_password", return_value=False):
            assert mod._verify_password_pam("hermes-user", "lo-que-sea") is False

    def test_empty_password_always_rejected(self) -> None:
        assert mod._verify_password_pam("hermes-user", "") is False
