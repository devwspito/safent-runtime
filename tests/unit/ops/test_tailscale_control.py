"""Tests for the hermes-tailscale-control root helper script (022).

Mirrors the pattern of test_remote_access_control_script.py: import the script
via path, mock subprocess/PAM for the bulk of the logic, and use a FAKE
`tailscale` binary on PATH for the two properties that must be proven at the
subprocess boundary itself (not just asserted on the mocked call):

  - the auth key NEVER reaches argv/env — only `--auth-key=file:<path>`, and
    the file is GONE once the helper returns (shredded, win or lose).
  - the staged request file is shredded IMMEDIATELY after a successful read —
    before any privileged action is attempted (contracts.md §3.1 step 1).

Contract: specs/022-tailnet-connectivity/contracts.md §3 (binding, the egress
lane's shell_server/tailnet/api.py is already implemented against it):
  connect    — {"action": "connect", "auth_key": "..."} — NO password field.
  disconnect — {"action": "disconnect", "password": "..."} — PAM-gated.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    / "hermes-tailscale-control"
)


def _load_script():
    loader = importlib.machinery.SourceFileLoader("hermes_tailscale_control", str(_SCRIPT_PATH))
    spec = importlib.util.spec_from_file_location(
        "hermes_tailscale_control", _SCRIPT_PATH, loader=loader
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_tailscale_control"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_script()


# ---------------------------------------------------------------------------
# Fixtures — redirect every module path constant into tmp_path.
# ---------------------------------------------------------------------------


@pytest.fixture()
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    control_dir = tmp_path / "tailscale-control"
    control_dir.mkdir(mode=0o700)
    runtime_dir = tmp_path / "run-tailscale"
    runtime_dir.mkdir(mode=0o711)
    state_dir = tmp_path / "state-tailscale"
    state_dir.mkdir(mode=0o700)
    private_tmp_dir = tmp_path / "private-tmp"
    private_tmp_dir.mkdir(mode=0o700)

    stage_file = control_dir / "request.json"
    status_file = runtime_dir / "status.json"
    enabled_marker = state_dir / "enabled"
    socket_path = runtime_dir / "tailscaled.sock"
    authkey_file = private_tmp_dir / "hermes-tailscale-authkey"
    kill_switch_result_file = control_dir / "kill-switch-result.json"
    last_attempt_file = runtime_dir / "last-attempt.json"

    monkeypatch.setattr(mod, "STAGE_DIR", control_dir)
    monkeypatch.setattr(mod, "STAGE_FILE", stage_file)
    monkeypatch.setattr(mod, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(mod, "STATUS_FILE", status_file)
    monkeypatch.setattr(mod, "ENABLED_MARKER", enabled_marker)
    monkeypatch.setattr(mod, "TS_SOCKET", socket_path)
    monkeypatch.setattr(mod, "AUTHKEY_FILE", authkey_file)
    monkeypatch.setattr(mod, "KILL_SWITCH_RESULT_FILE", kill_switch_result_file)
    monkeypatch.setattr(mod, "LAST_ATTEMPT_FILE", last_attempt_file)

    return {
        "control_dir": control_dir,
        "runtime_dir": runtime_dir,
        "state_dir": state_dir,
        "stage_file": stage_file,
        "status_file": status_file,
        "enabled_marker": enabled_marker,
        "last_attempt_file": last_attempt_file,
        "socket_path": socket_path,
        "authkey_file": authkey_file,
        "private_tmp_dir": private_tmp_dir,
        "kill_switch_result_file": kill_switch_result_file,
    }


def _make_staged_file(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# _read_staged TOCTOU checks
# ---------------------------------------------------------------------------


class TestReadStagedValidation:
    def test_refuses_wrong_uid(self, paths: dict) -> None:
        _make_staged_file(paths["stage_file"], {"action": "connect"})
        with patch.object(mod, "_hermes_uid", return_value=99999):
            with pytest.raises(ValueError, match="uid"):
                mod._read_staged()

    def test_refuses_wrong_mode(self, paths: dict) -> None:
        paths["stage_file"].write_text('{"action":"connect"}', encoding="utf-8")
        os.chmod(paths["stage_file"], 0o644)
        with patch.object(mod, "_hermes_uid", return_value=os.getuid()):
            with pytest.raises(ValueError, match="mode"):
                mod._read_staged()

    def test_accepts_correct_uid_and_mode(self, paths: dict) -> None:
        _make_staged_file(paths["stage_file"], {"action": "connect"})
        with patch.object(mod, "_hermes_uid", return_value=os.getuid()):
            assert mod._read_staged() == {"action": "connect"}


# ---------------------------------------------------------------------------
# _apply dispatch — connect needs NO password (contracts.md §3.1/§6);
# disconnect DOES (contracts.md §3.2).
# ---------------------------------------------------------------------------


class TestApplyDispatch:
    def test_connect_never_touches_pam(self) -> None:
        with (
            patch.object(mod, "_connect", return_value=True) as mock_connect,
            patch.object(mod, "_verify_password_pam") as mock_pam,
        ):
            rc = mod._apply({"action": "connect", "auth_key": "tskey-x"})
        assert rc == 0
        mock_connect.assert_called_once()
        mock_pam.assert_not_called()

    def test_connect_failure_returns_1(self) -> None:
        with patch.object(mod, "_connect", return_value=False):
            rc = mod._apply({"action": "connect", "auth_key": "tskey-x"})
        assert rc == 1

    def test_disconnect_with_correct_password_calls_disconnect(self) -> None:
        with (
            patch.object(mod, "_verify_password_pam", return_value=True),
            patch.object(mod, "_disconnect", return_value=True) as mock_disconnect,
        ):
            rc = mod._apply({"action": "disconnect", "password": "correct"})
        assert rc == 0
        mock_disconnect.assert_called_once()

    def test_disconnect_with_wrong_password_never_calls_disconnect_and_exits_0(
        self,
    ) -> None:
        """025 re-verificación d2eb8c6 (menor nuevo): a rejected PAM password
        is a normal, fail-closed policy outcome — the caller (tailnet/api.py)
        never inspects this unit's exit code, it polls GET /tailnet instead —
        not a systemd unit failure. Before this fix rc==1 left
        hermes-tailscale-control.service `failed` permanently after every
        single wrong-password attempt."""
        with (
            patch.object(mod, "_verify_password_pam", return_value=False),
            patch.object(mod, "_disconnect") as mock_disconnect,
        ):
            rc = mod._apply({"action": "disconnect", "password": "wrong"})
        assert rc == 0
        mock_disconnect.assert_not_called()

    def test_disconnect_without_password_rejected_before_pam(self) -> None:
        with (
            patch.object(mod, "_verify_password_pam") as mock_pam,
            patch.object(mod, "_disconnect") as mock_disconnect,
        ):
            rc = mod._apply({"action": "disconnect"})
        assert rc == 1
        mock_pam.assert_not_called()
        mock_disconnect.assert_not_called()

    def test_kill_switch_release_with_correct_password_calls_release(self, paths: dict) -> None:
        with (
            patch.object(mod, "_verify_password_pam", return_value=True),
        ):
            rc = mod._apply({"action": "kill_switch_release", "password": "correct"})
        assert rc == 0

    def test_kill_switch_release_with_wrong_password_returns_0_not_a_unit_failure(
        self, paths: dict
    ) -> None:
        """025 re-verificación d2eb8c6 (FALLA menor): a rejected release is
        reported via kill-switch-result.json (security_api.py polls THAT
        file, never the unit's exit code/systemctl Result) — reproduced live
        on the DGX as hermes-tailscale-control.service stuck `failed`
        forever after a single wrong-password attempt. Exit 0; the true
        verdict still lands in the result file."""
        with patch.object(mod, "_verify_password_pam", return_value=False):
            rc = mod._apply({"action": "kill_switch_release", "password": "wrong"})
        assert rc == 0
        result = json.loads(paths["kill_switch_result_file"].read_text(encoding="utf-8"))
        assert result["ok"] is False

    def test_unknown_action_returns_1_without_pam(self) -> None:
        with patch.object(mod, "_verify_password_pam") as mock_pam:
            rc = mod._apply({"action": "reboot"})
        assert rc == 1
        mock_pam.assert_not_called()

    def test_status_action_no_longer_exists(self) -> None:
        """contracts.md §2/§7: status.json is the watcher's job now, not the
        control script's — a stray "status" action must be rejected like any
        other unknown action, never silently accepted."""
        rc = mod._apply({"action": "status"})
        assert rc == 1

    def test_missing_action_returns_1(self) -> None:
        assert mod._apply({}) == 1


# ---------------------------------------------------------------------------
# _connect: auth_key validation + write/shred ordering
# ---------------------------------------------------------------------------


class TestConnect:
    def test_missing_auth_key_writes_nothing(self, paths: dict) -> None:
        with (
            patch.object(mod, "_write_enabled_marker") as mock_marker,
            patch.object(mod, "_write_authkey_file") as mock_authkey,
        ):
            ok = mod._connect({"action": "connect"})
        assert ok is False
        mock_marker.assert_not_called()
        mock_authkey.assert_not_called()

    def test_empty_auth_key_rejected(self, paths: dict) -> None:
        assert mod._connect({"auth_key": ""}) is False

    def test_multiline_auth_key_rejected(self, paths: dict) -> None:
        assert mod._connect({"auth_key": "tskey-x\nmalicious"}) is False

    def test_valid_key_writes_marker_and_authkey_then_shreds_on_success(self, paths: dict) -> None:
        with patch.object(mod, "_tailscale_up_with_retry", return_value=True) as mock_up:
            ok = mod._connect({"auth_key": "tskey-abc123"})
        assert ok is True
        assert paths["enabled_marker"].exists()
        assert not paths["authkey_file"].exists(), "authkey must be shredded after use"
        mock_up.assert_called_once()

    def test_shreds_authkey_even_when_tailscale_up_fails(self, paths: dict) -> None:
        with patch.object(mod, "_tailscale_up_with_retry", return_value=False):
            ok = mod._connect({"auth_key": "tskey-abc123"})
        assert ok is False
        assert not paths["authkey_file"].exists()

    def test_authkey_file_is_0600(self, paths: dict) -> None:
        mod._write_authkey_file("tskey-secret")
        mode = stat.S_IMODE(paths["authkey_file"].stat().st_mode)
        assert mode == 0o600
        assert paths["authkey_file"].read_text(encoding="utf-8") == "tskey-secret"

    def test_authkey_lives_under_private_tmp_not_the_shared_runtime_dir(self, paths: dict) -> None:
        """contracts.md §3.1 step 2: NEVER back into /run/hermes/tailscale
        (shared, 0711 since §2) — a private location the helper alone controls."""
        assert str(paths["runtime_dir"]) not in str(mod.AUTHKEY_FILE)


# ---------------------------------------------------------------------------
# last_attempt (025 hallazgo D): a rejected key must NOT read as "configured"
# — this script records its own verdict, the status watcher mirrors it.
# ---------------------------------------------------------------------------


class TestConnectLastAttempt:
    def test_success_writes_ok_true_no_error_kind(self, paths: dict) -> None:
        with patch.object(mod, "_tailscale_up_with_retry", return_value=True):
            mod._connect({"auth_key": "tskey-abc123"})
        result = json.loads(paths["last_attempt_file"].read_text(encoding="utf-8"))
        assert result["ok"] is True
        assert result["error_kind"] is None
        assert result["at"]

    def test_failure_writes_ok_false_with_error_kind(self, paths: dict) -> None:
        with patch.object(mod, "_tailscale_up_with_retry", return_value=False):
            mod._connect({"auth_key": "tskey-abc123"})
        result = json.loads(paths["last_attempt_file"].read_text(encoding="utf-8"))
        assert result["ok"] is False
        assert result["error_kind"] == "tailscale_up_failed"

    def test_last_attempt_never_contains_the_auth_key(self, paths: dict) -> None:
        with patch.object(mod, "_tailscale_up_with_retry", return_value=False):
            mod._connect({"auth_key": "tskey-should-never-appear-here"})
        raw = paths["last_attempt_file"].read_text(encoding="utf-8")
        assert "tskey" not in raw
        assert "should-never-appear" not in raw

    def test_last_attempt_file_is_0644(self, paths: dict) -> None:
        with patch.object(mod, "_tailscale_up_with_retry", return_value=True):
            mod._connect({"auth_key": "tskey-abc123"})
        mode = stat.S_IMODE(paths["last_attempt_file"].stat().st_mode)
        assert mode == 0o644

    def test_a_later_success_overwrites_an_earlier_failure(self, paths: dict) -> None:
        with patch.object(mod, "_tailscale_up_with_retry", return_value=False):
            mod._connect({"auth_key": "tskey-first-rejected"})
        assert json.loads(paths["last_attempt_file"].read_text(encoding="utf-8"))["ok"] is False

        with patch.object(mod, "_tailscale_up_with_retry", return_value=True):
            mod._connect({"auth_key": "tskey-second-accepted"})
        assert json.loads(paths["last_attempt_file"].read_text(encoding="utf-8"))["ok"] is True


# ---------------------------------------------------------------------------
# _disconnect — PAM already verified by the time this runs; removes the
# marker, stops tailscaled, and deletes status.json (the watcher that would
# normally refresh it dies WITH tailscaled — BindsTo — so it never gets a
# last tick to report "offline").
# ---------------------------------------------------------------------------


class TestDisconnect:
    def test_removes_marker_stops_unit_and_deletes_status_json(self, paths: dict) -> None:
        paths["enabled_marker"].write_text("1\n", encoding="utf-8")
        paths["status_file"].write_text('{"online": true}', encoding="utf-8")
        with (
            patch.object(mod, "_run_tailscale", return_value=MagicMock(returncode=0)) as mock_run,
            patch.object(mod, "_systemctl", return_value=True) as mock_systemctl,
        ):
            ok = mod._disconnect()
        assert ok is True
        assert not paths["enabled_marker"].exists()
        assert not paths["status_file"].exists(), "stale status.json must not survive a disconnect"
        mock_systemctl.assert_called_once_with(["stop", "hermes-tailscaled.service"])
        mock_run.assert_called_once_with(["logout"], timeout=mod._LOGOUT_SUBPROCESS_TIMEOUT)

    def test_marker_and_status_file_absent_is_not_an_error(self, paths: dict) -> None:
        with (
            patch.object(mod, "_run_tailscale", return_value=None),
            patch.object(mod, "_systemctl", return_value=True),
        ):
            ok = mod._disconnect()
        assert ok is True


# ---------------------------------------------------------------------------
# _kill_switch_release (025 hallazgo C): reuses THIS script's PAM gate for the
# emergency-brake release when the owner hasn't enrolled TOTP. Verifies and
# writes a result file — it never touches tailscale/systemctl (releasing the
# brake is security_api.py's D-Bus call, this script only proves the password).
# ---------------------------------------------------------------------------


class TestKillSwitchRelease:
    def test_correct_password_writes_ok_true_result(self, paths: dict) -> None:
        with patch.object(mod, "_verify_password_pam", return_value=True):
            ok = mod._kill_switch_release({"password": "correct"})
        assert ok is True
        result = json.loads(paths["kill_switch_result_file"].read_text(encoding="utf-8"))
        assert result["ok"] is True

    def test_wrong_password_writes_ok_false_result(self, paths: dict) -> None:
        with patch.object(mod, "_verify_password_pam", return_value=False):
            ok = mod._kill_switch_release({"password": "wrong"})
        assert ok is False
        result = json.loads(paths["kill_switch_result_file"].read_text(encoding="utf-8"))
        assert result["ok"] is False

    def test_missing_password_never_calls_pam_and_writes_ok_false(self, paths: dict) -> None:
        with patch.object(mod, "_verify_password_pam") as mock_pam:
            ok = mod._kill_switch_release({})
        assert ok is False
        mock_pam.assert_not_called()
        result = json.loads(paths["kill_switch_result_file"].read_text(encoding="utf-8"))
        assert result["ok"] is False

    def test_result_file_is_0600(self, paths: dict) -> None:
        with patch.object(mod, "_verify_password_pam", return_value=True):
            mod._kill_switch_release({"password": "correct"})
        mode = stat.S_IMODE(paths["kill_switch_result_file"].stat().st_mode)
        assert mode == 0o600

    def test_never_touches_tailscale_or_systemctl(self, paths: dict) -> None:
        with (
            patch.object(mod, "_verify_password_pam", return_value=True),
            patch.object(mod, "_run_tailscale") as mock_run,
            patch.object(mod, "_systemctl") as mock_systemctl,
        ):
            mod._kill_switch_release({"password": "correct"})
        mock_run.assert_not_called()
        mock_systemctl.assert_not_called()


# ---------------------------------------------------------------------------
# _tailscale_up_with_retry — capped retries, backoff+jitter, no real sleep
# ---------------------------------------------------------------------------


class TestTailscaleUpRetry:
    def test_succeeds_on_first_attempt_no_sleep(self, paths: dict) -> None:
        with (
            patch.object(mod, "_run_tailscale", return_value=MagicMock(returncode=0)) as mock_run,
            patch("time.sleep") as mock_sleep,
        ):
            ok = mod._tailscale_up_with_retry()
        assert ok is True
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    def test_retries_up_to_the_cap_then_gives_up(self, paths: dict) -> None:
        with (
            patch.object(mod, "_run_tailscale", return_value=MagicMock(returncode=1)) as mock_run,
            patch("time.sleep") as mock_sleep,
        ):
            ok = mod._tailscale_up_with_retry()
        assert ok is False
        assert mock_run.call_count == mod._UP_MAX_ATTEMPTS
        assert mock_sleep.call_count == mod._UP_MAX_ATTEMPTS - 1

    def test_succeeds_after_transient_failures(self, paths: dict) -> None:
        results = [MagicMock(returncode=1), MagicMock(returncode=1), MagicMock(returncode=0)]
        with (
            patch.object(mod, "_run_tailscale", side_effect=results),
            patch("time.sleep"),
        ):
            ok = mod._tailscale_up_with_retry()
        assert ok is True

    def test_up_command_never_puts_the_raw_key_in_argv(self, paths: dict) -> None:
        with patch.object(mod, "_run_tailscale", return_value=MagicMock(returncode=0)) as mock_run:
            mod._tailscale_up_with_retry()
        (args,), kwargs = mock_run.call_args
        joined = " ".join(args)
        assert "--auth-key=file:" in joined
        assert "tskey-" not in joined  # no raw key literal ever appears in argv


# ---------------------------------------------------------------------------
# main(): root gate + shred IMMEDIATELY after read (before acting)
# ---------------------------------------------------------------------------


class TestMain:
    def test_requires_root(self, paths: dict) -> None:
        with patch("os.geteuid", return_value=1000):
            rc = mod.main()
        assert rc == 1

    def test_no_staged_file_is_a_noop(self, paths: dict) -> None:
        with patch("os.geteuid", return_value=0):
            rc = mod.main()
        assert rc == 0

    def test_shreds_staged_file_on_parse_failure(self, paths: dict) -> None:
        paths["stage_file"].write_text("NOT VALID JSON", encoding="utf-8")
        os.chmod(paths["stage_file"], 0o600)
        with patch("os.geteuid", return_value=0):
            rc = mod.main()
        assert rc == 1
        assert not paths["stage_file"].exists()

    def test_shreds_staged_file_before_the_privileged_action_runs(self, paths: dict) -> None:
        """contracts.md §3.1 step 1: shred happens BEFORE `tailscale up` is
        even attempted — proven by asserting the file is already gone from
        INSIDE the mocked _connect call, not just after main() returns."""
        _make_staged_file(paths["stage_file"], {"action": "connect", "auth_key": "tskey-x"})
        seen_stage_file_exists = []

        def _fake_connect(staged: dict) -> bool:
            seen_stage_file_exists.append(paths["stage_file"].exists())
            return True

        with (
            patch.object(mod, "_hermes_uid", return_value=os.getuid()),
            patch.object(mod, "_connect", side_effect=_fake_connect),
            patch("os.geteuid", return_value=0),
        ):
            rc = mod.main()

        assert rc == 0
        assert seen_stage_file_exists == [False], "the staged file must already be gone by _connect time"

    def test_disconnect_flow_end_to_end_with_mocks(self, paths: dict) -> None:
        _make_staged_file(paths["stage_file"], {"action": "disconnect", "password": "correct"})
        with (
            patch.object(mod, "_hermes_uid", return_value=os.getuid()),
            patch.object(mod, "_verify_password_pam", return_value=True),
            patch.object(mod, "_disconnect", return_value=True) as mock_disconnect,
            patch("os.geteuid", return_value=0),
        ):
            rc = mod.main()
        assert rc == 0
        mock_disconnect.assert_called_once()
        assert not paths["stage_file"].exists()


# ---------------------------------------------------------------------------
# Fake `tailscale` binary on PATH — proves the argv/file-lifecycle properties
# at the real subprocess boundary, not just on the mock.
# ---------------------------------------------------------------------------

_FAKE_TAILSCALE = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TAILSCALE_LOG"
subcmd=""
authkey_flag=""
for a in "$@"; do
  case "$a" in
    up|status|logout) subcmd="$a" ;;
    --auth-key=*) authkey_flag="$a" ;;
  esac
done
if [ -n "$authkey_flag" ]; then
  printf 'AUTHKEY_FLAG:%s\\n' "$authkey_flag" >> "$FAKE_TAILSCALE_LOG"
  path="${authkey_flag#--auth-key=file:}"
  if [ -f "$path" ]; then
    printf 'AUTHKEY_FILE_EXISTED_AT_CALL\\n' >> "$FAKE_TAILSCALE_LOG"
  fi
fi
case "$subcmd" in
  up) exit "${FAKE_TAILSCALE_UP_RC:-0}" ;;
  status) cat "$FAKE_TAILSCALE_STATUS_JSON"; exit 0 ;;
  logout) exit 0 ;;
esac
exit 0
"""


@pytest.fixture()
def fake_tailscale_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    ts = bin_dir / "tailscale"
    ts.write_text(_FAKE_TAILSCALE)
    ts.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    log = tmp_path / "fake-tailscale.log"
    monkeypatch.setenv("FAKE_TAILSCALE_LOG", str(log))
    return log


class TestFakeBinaryOnPathProvesArgvAndFileLifecycle:
    def test_connect_calls_real_subprocess_with_file_flag_and_shreds_after(
        self, paths: dict, fake_tailscale_on_path: Path
    ) -> None:
        ok = mod._connect({"auth_key": "tskey-abc123-should-never-be-in-argv"})
        assert ok is True

        log_text = fake_tailscale_on_path.read_text(encoding="utf-8")
        assert "--auth-key=file:" in log_text
        assert "tskey-abc123-should-never-be-in-argv" not in log_text
        assert "AUTHKEY_FILE_EXISTED_AT_CALL" in log_text  # the file existed WHEN tailscale read it

        # And the file is gone now that the helper has returned.
        assert not paths["authkey_file"].exists()

    def test_disconnect_calls_logout_via_real_subprocess(
        self, paths: dict, fake_tailscale_on_path: Path
    ) -> None:
        paths["enabled_marker"].write_text("1\n", encoding="utf-8")
        with patch.object(mod, "_systemctl", return_value=True):
            ok = mod._disconnect()
        assert ok is True
        log_text = fake_tailscale_on_path.read_text(encoding="utf-8")
        assert "logout" in log_text
