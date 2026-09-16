"""`safent brake release` (R17, spec 025 matriz item R17) — host-CLI
sovereign fallback to release the emergency brake without MFA.

Before this verb existed, an owner on a fresh install (brake engaged, no
TOTP enrolled, no device/OS password set) had NO release path at all:
`POST /api/v1/security/kill-switch` fails closed 403 `invalid_device_
password` in that exact state. This verb reaches the container the same
way every other host verb here does (`podman exec`, no HTTP, no bearer) —
host access to run it IS the owner (see brake_release_cli.py's own
docstring for the full authorization chain).

Runs the REAL `safent` CLI (POSIX sh) as a subprocess, with only `podman`
faked (a logging shim on PATH), exactly like test_safent_cli_backup_restore.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAFENT_CLI = _REPO_ROOT / "safent"

_FAKE_PODMAN = """#!/usr/bin/env bash
set -e
echo "$@" >> "$FAKE_PODMAN_LOG"

case "$1" in
  inspect)
    [ "$FAKE_CONTAINER_EXISTS" = "true" ] || exit 1
    template=""
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        -f) template="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    case "$template" in
      *State.Running*) [ "$FAKE_CONTAINER_RUNNING" = "true" ] && echo true || echo false ;;
    esac
    exit 0
    ;;
  exec)
    exit "${FAKE_EXEC_EXIT:-0}"
    ;;
  *)
    exit 0
    ;;
esac
"""


@pytest.fixture()
def fake_bin_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text(_FAKE_PODMAN)
    podman.chmod(0o755)
    return bin_dir


def _run_safent(
    *args: str,
    fake_bin_dir: Path,
    home_dir: Path,
    podman_log: Path,
    container_exists: bool = True,
    container_running: bool = True,
    exec_exit: int = 0,
) -> subprocess.CompletedProcess[str]:
    home_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin_dir}:{env.get('PATH', '')}"
    env["HOME"] = str(home_dir)
    env["SAFENT_NAME"] = "safent-test"
    env["FAKE_PODMAN_LOG"] = str(podman_log)
    env["FAKE_CONTAINER_EXISTS"] = "true" if container_exists else "false"
    env["FAKE_CONTAINER_RUNNING"] = "true" if container_running else "false"
    env["FAKE_EXEC_EXIT"] = str(exec_exit)
    return subprocess.run(
        ["sh", str(_SAFENT_CLI), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _podman_calls(podman_log: Path) -> list[str]:
    if not podman_log.exists():
        return []
    return [line for line in podman_log.read_text().splitlines() if line]


class TestBrakeReleaseUsage:
    def test_no_subcommand_is_a_usage_error(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        result = _run_safent(
            "brake",
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=tmp_path / "podman.log",
        )
        assert result.returncode != 0
        assert "usage" in (result.stdout + result.stderr).lower()

    def test_unknown_subcommand_is_rejected(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        result = _run_safent(
            "brake", "bogus",
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=tmp_path / "podman.log",
        )
        assert result.returncode != 0
        assert "unknown" in (result.stdout + result.stderr).lower()


class TestBrakeReleaseRequiresARunningContainer:
    def test_refuses_when_safent_is_not_running(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        podman_log = tmp_path / "podman.log"
        result = _run_safent(
            "brake", "release",
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            container_exists=False,
            container_running=False,
        )
        assert result.returncode != 0
        assert "must be running" in (result.stdout + result.stderr).lower()
        assert not any(c.startswith("exec ") for c in _podman_calls(podman_log)), _podman_calls(podman_log)


class TestBrakeReleaseExecsAsHermesUser:
    def test_execs_the_release_cli_as_hermes_user_not_root(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """R17's whole audit-attribution argument depends on this EXACT uid:
        hermes-user (1000) is the uid the daemon's authorized_uids already
        trusts directly and the uid HERMES_OPERATOR_ID encodes — execing as
        root or as the `hermes` service uid would attribute the release to
        the wrong (or no) identity."""
        podman_log = tmp_path / "podman.log"
        result = _run_safent(
            "brake", "release",
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            container_exists=True,
            container_running=True,
            exec_exit=0,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        calls = _podman_calls(podman_log)
        exec_calls = [c for c in calls if c.startswith("exec ")]
        assert len(exec_calls) == 1, calls
        assert "-u hermes-user" in exec_calls[0], exec_calls[0]
        assert "hermes.shell_server.security.brake_release_cli release" in exec_calls[0], exec_calls[0]

    def test_a_failing_release_is_a_clear_cli_error_not_a_silent_success(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        result = _run_safent(
            "brake", "release",
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            container_exists=True,
            container_running=True,
            exec_exit=1,
        )
        assert result.returncode != 0
        assert "could not release the brake" in (result.stdout + result.stderr).lower()
