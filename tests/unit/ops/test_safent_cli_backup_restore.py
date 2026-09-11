"""`safent backup` / `safent restore` — argument handling and refusal paths
(item #3, spec 025 matriz — backup/restore was entirely missing).

Runs the REAL `safent` CLI (POSIX sh) as a subprocess, with only `podman`
faked (a logging shim on PATH — see _FAKE_PODMAN); `tar`, the checksum tool,
`date`, `mktemp` are the real host tools, exactly like a real run. `HOME` is
redirected to an isolated tmp dir so nothing ever touches the real
`~/.safent`.

Covers:
  - backup refuses when there is no data volume to back up (nothing to
    stop/archive/restart).
  - a successful backup produces a 0600 archive containing manifest.json +
    data-volume.tar + state.tar, with sha256 entries that match the actual
    file contents.
  - backup only restarts Safent if it was running before (no surprise
    start on an already-stopped instance).
  - restore's usage error (no archive argument) and missing-file error.
  - restore REFUSES to overwrite an existing data volume without --force —
    the critical safety path — and does so WITHOUT calling `podman volume
    rm`/`import` (verified via the podman invocation log, not just the
    process exit code).
  - restore proceeds and imports the volume when --force is given.
  - restore refuses a tampered archive (sha256 mismatch) without ever
    calling `podman volume rm`/`import`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAFENT_CLI = _REPO_ROOT / "safent"

_FAKE_PODMAN = """#!/usr/bin/env bash
set -e
echo "$@" >> "$FAKE_PODMAN_LOG"

# Stateful container existence/running flags (BKP-01 regression tests need
# `start`/`run`/`stop` to actually flip what a later `inspect` reports —
# a purely static double can't tell "cmd_start's own dispatch was wrong"
# apart from "cmd_start dispatched correctly but the postcondition check
# is missing", which is exactly the bug this CLI had). Seeded from the
# FAKE_CONTAINER_* env vars, then mutated on disk by start/run/stop.
_exists_file="$FAKE_STATE_DIR/exists"
_running_file="$FAKE_STATE_DIR/running"
[ -f "$_exists_file" ] || echo "$FAKE_CONTAINER_EXISTS" > "$_exists_file"
[ -f "$_running_file" ] || echo "$FAKE_CONTAINER_RUNNING" > "$_running_file"

case "$1" in
  ps)
    [ "${FAKE_PS_FAILS:-}" != "true" ] || exit 125
    if [ "$(cat "$_exists_file")" = "true" ]; then echo safent-test; fi
    exit 0
    ;;
  inspect)
    exists="$(cat "$_exists_file")"
    [ "$exists" = "true" ] || exit 1
    template=""
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        -f) template="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    case "$template" in
      *State.Running*) cat "$_running_file" ;;
      "") : ;;
      *) echo "" ;;
    esac
    exit 0
    ;;
  volume)
    case "$2" in
      exists)
        [ "$FAKE_VOLUME_EXISTS" = "true" ] && exit 0 || exit 1
        ;;
      export)
        out=""
        shift 3
        while [ $# -gt 0 ]; do
          case "$1" in
            -o) out="$2"; shift 2 ;;
            *) shift ;;
          esac
        done
        printf 'FAKE-VOLUME-DATA' > "$out"
        exit 0
        ;;
      rm)
        [ "${FAKE_REMOVE_FAILS:-}" != "true" ] || exit 1
        exit 0
        ;;
      import|create)
        exit 0
        ;;
      *)
        exit 0
        ;;
    esac
    ;;
  stop)
    [ "${FAKE_STOP_FAILS:-}" != "true" ] || exit 1
    echo false > "$_running_file"
    exit 0
    ;;
  start)
    [ "${FAKE_START_FAILS:-}" != "true" ] || exit 1
    [ "$(cat "$_exists_file")" = "true" ] || exit 1
    echo true > "$_running_file"
    exit 0
    ;;
  run)
    if [ "${FAKE_RUN_FAILS:-}" = "true" ]; then exit 1; fi
    echo true > "$_exists_file"
    echo true > "$_running_file"
    exit 0
    ;;
  pull)
    exit 0
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
    container_running: bool = False,
    volume_exists: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    home_dir.mkdir(parents=True, exist_ok=True)
    state_dir = podman_log.parent / f"{podman_log.stem}-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin_dir}:{env.get('PATH', '')}"
    env["HOME"] = str(home_dir)
    env["SAFENT_NAME"] = "safent-test"
    env["SAFENT_DATA_VOLUME"] = "safent-test-data"
    env["FAKE_PODMAN_LOG"] = str(podman_log)
    env["FAKE_STATE_DIR"] = str(state_dir)
    env["FAKE_CONTAINER_EXISTS"] = "true" if container_exists else "false"
    env["FAKE_CONTAINER_RUNNING"] = "true" if container_running else "false"
    env["FAKE_VOLUME_EXISTS"] = "true" if volume_exists else "false"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["sh", str(_SAFENT_CLI), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _podman_calls(podman_log: Path) -> list[str]:
    if not podman_log.exists():
        return []
    return [line for line in podman_log.read_text().splitlines() if line]


class TestBackupRefusesWithoutAVolume:
    def test_no_volume_to_back_up_is_a_clean_error_not_a_crash(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        result = _run_safent(
            "backup",
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=tmp_path / "podman.log",
            volume_exists=False,
        )
        assert result.returncode != 0
        assert "nothing to do" in result.stdout.lower() or "nothing to do" in result.stderr.lower()


class TestSuccessfulBackup:
    def test_archive_is_private_before_final_chmod(
        self, tmp_path: Path, fake_bin_dir: Path,
    ) -> None:
        tar = fake_bin_dir / "tar"
        tar.write_text(
            '#!/bin/sh\n"$REAL_TAR" "$@" || exit $?\n'
            'if [ "$1" = -czf ]; then ls -l "$2" > "$ARCHIVE_MODE_LOG"; fi\n'
        )
        tar.chmod(0o755)
        mode_log = tmp_path / "archive-mode"
        result = _run_safent(
            "backup", str(tmp_path / "backups"), fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home", podman_log=tmp_path / "podman.log",
            extra_env={"REAL_TAR": shutil.which("tar"), "ARCHIVE_MODE_LOG": str(mode_log)},
        )
        assert result.returncode == 0, result.stderr
        assert mode_log.read_text().startswith("-rw-------")

    @pytest.mark.parametrize("symlink", [False, True])
    def test_existing_destination_is_not_overwritten(
        self, tmp_path: Path, fake_bin_dir: Path, symlink: bool,
    ) -> None:
        date = fake_bin_dir / "date"
        date.write_text('#!/bin/sh\nprintf "20260911T000000Z\\n"\n')
        date.chmod(0o755)
        output = tmp_path / "backups"
        output.mkdir()
        destination = output / "safent-backup-20260911T000000Z.tar.gz"
        protected = tmp_path / "protected"
        protected.write_bytes(b"must survive")
        if symlink:
            destination.symlink_to(protected)
        else:
            destination.write_bytes(b"must survive")
        result = _run_safent(
            "backup", str(output), fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home", podman_log=tmp_path / "podman.log",
        )
        assert result.returncode != 0
        assert destination.read_bytes() == b"must survive"
        assert protected.read_bytes() == b"must survive"

    def test_unavailable_engine_is_not_proof_of_stopped_data(
        self, tmp_path: Path, fake_bin_dir: Path,
    ) -> None:
        log = tmp_path / "podman.log"
        result = _run_safent(
            "backup", str(tmp_path / "backups"), fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home", podman_log=log,
            extra_env={"FAKE_PS_FAILS": "true"},
        )
        assert result.returncode != 0
        assert not any(call.startswith("volume export") for call in _podman_calls(log))

    def test_failed_stop_never_exports_live_data(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        log = tmp_path / "podman.log"
        result = _run_safent(
            "backup", str(tmp_path / "backups"), fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home", podman_log=log, container_running=True,
            extra_env={"FAKE_STOP_FAILS": "true"},
        )
        assert result.returncode != 0
        assert not any(call.startswith("volume export") for call in _podman_calls(log))
        assert "[ok] backup" not in result.stdout.lower()

    def test_restart_failure_reports_archive_without_claiming_full_success(
        self, tmp_path: Path, fake_bin_dir: Path,
    ) -> None:
        output = tmp_path / "backups"
        result = _run_safent(
            "backup", str(output), fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home", podman_log=tmp_path / "podman.log",
            container_running=True, extra_env={"FAKE_START_FAILS": "true"},
        )
        assert result.returncode != 0
        assert len(list(output.glob("safent-backup-*.tar.gz"))) == 1
        assert "restart" in (result.stdout + result.stderr).lower()
        assert "[ok] backup" not in result.stdout.lower()

    def test_produces_a_0600_archive_with_manifest_and_matching_checksums(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        out_dir = tmp_path / "backups"
        result = _run_safent(
            "backup", str(out_dir),
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=tmp_path / "podman.log",
            container_exists=True,
            container_running=True,
            volume_exists=True,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

        archives = list(out_dir.glob("safent-backup-*.tar.gz"))
        assert len(archives) == 1
        archive = archives[0]
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        with tarfile.open(archive) as tf:
            tf.extractall(extract_dir, filter="data")

        manifest = json.loads((extract_dir / "manifest.json").read_text())
        for member in ("data-volume.tar", "state.tar"):
            actual = hashlib.sha256((extract_dir / member).read_bytes()).hexdigest()
            assert manifest["sha256"][member] == actual, member
        assert manifest["data_volume"] == "safent-test-data"

    def test_restarts_only_if_it_was_running_before(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        result = _run_safent(
            "backup",
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            container_exists=True,
            container_running=False,  # already stopped BEFORE the backup
            volume_exists=True,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        calls = _podman_calls(podman_log)
        assert not any(c.startswith("start ") for c in calls), calls


class TestRestoreArgumentHandling:
    def test_no_archive_argument_is_a_usage_error(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        result = _run_safent(
            "restore",
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=tmp_path / "podman.log",
        )
        assert result.returncode != 0
        assert "usage" in (result.stdout + result.stderr).lower()

    def test_missing_archive_file_errors_before_touching_podman(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        result = _run_safent(
            "restore", str(tmp_path / "does-not-exist.tar.gz"),
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
        )
        assert result.returncode != 0
        assert "not found" in (result.stdout + result.stderr).lower()
        assert _podman_calls(podman_log) == []


def _make_backup(
    tmp_path: Path, fake_bin_dir: Path, out_dir: Path
) -> Path:
    result = _run_safent(
        "backup", str(out_dir),
        fake_bin_dir=fake_bin_dir,
        home_dir=tmp_path / "home-for-backup",
        podman_log=tmp_path / "podman-backup.log",
        container_exists=True,
        container_running=True,
        volume_exists=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    (archive,) = out_dir.glob("safent-backup-*.tar.gz")
    return archive


class TestRestoreRefusesToOverwriteWithoutForce:
    def test_failed_removal_never_imports_over_existing_data(
        self, tmp_path: Path, fake_bin_dir: Path,
    ) -> None:
        archive = _make_backup(tmp_path, fake_bin_dir, tmp_path / "backups")
        log = tmp_path / "podman-restore.log"
        result = _run_safent(
            "restore", str(archive), "--force", fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "restore-home", podman_log=log,
            extra_env={"FAKE_REMOVE_FAILS": "true"},
        )
        assert result.returncode != 0
        assert not any(call.startswith("volume import") for call in _podman_calls(log))

    def test_failed_stop_never_removes_or_imports_volume(
        self, tmp_path: Path, fake_bin_dir: Path,
    ) -> None:
        archive = _make_backup(tmp_path, fake_bin_dir, tmp_path / "backups")
        log = tmp_path / "podman-restore.log"
        result = _run_safent(
            "restore", str(archive), "--force", fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "restore-home", podman_log=log,
            container_running=True, extra_env={"FAKE_STOP_FAILS": "true"},
        )
        assert result.returncode != 0
        calls = _podman_calls(log)
        assert not any(call.startswith(("volume rm", "volume import")) for call in calls)

    def test_refuses_when_volume_exists_and_no_force(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        archive = _make_backup(tmp_path, fake_bin_dir, tmp_path / "backups")
        podman_log = tmp_path / "podman-restore.log"

        result = _run_safent(
            "restore", str(archive),
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home-for-restore",
            podman_log=podman_log,
            container_exists=True,
            container_running=True,
            volume_exists=True,  # the volume we'd be about to clobber
        )

        assert result.returncode != 0
        assert "force" in (result.stdout + result.stderr).lower()
        calls = _podman_calls(podman_log)
        # the refusal must be BEFORE any destructive step — no rm/import ever issued
        assert not any(c.startswith("volume rm") for c in calls), calls
        assert not any(c.startswith("volume import") for c in calls), calls
        # ...and no stop either: refuse fast, touch nothing
        assert not any(c.startswith("stop") for c in calls), calls

    def test_proceeds_and_imports_the_volume_with_force(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        archive = _make_backup(tmp_path, fake_bin_dir, tmp_path / "backups")
        podman_log = tmp_path / "podman-restore.log"

        # container_running=False: the fake reports a STATIC state (it does
        # not model stop/start transitions), so "already stopped" is what
        # exercises cmd_start's `podman start` branch at the end — matching
        # the real-world case (restore always stops first regardless).
        result = _run_safent(
            "restore", str(archive), "--force",
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home-for-restore",
            podman_log=podman_log,
            container_exists=True,
            container_running=False,
            volume_exists=True,
        )

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        calls = _podman_calls(podman_log)
        assert any(c.startswith("volume rm") for c in calls), calls
        assert any(c.startswith("volume create") for c in calls), calls
        assert any(c.startswith("volume import") for c in calls), calls
        assert any(c.startswith("start ") for c in calls), calls  # restarted at the end

    def test_force_before_the_archive_path_is_also_accepted(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """`--force` and the archive path may arrive in either order."""
        archive = _make_backup(tmp_path, fake_bin_dir, tmp_path / "backups")
        result = _run_safent(
            "restore", "--force", str(archive),
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home-for-restore",
            podman_log=tmp_path / "podman-restore.log",
            container_exists=True,
            container_running=True,
            volume_exists=True,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"


class TestRestoreVerifiesTheContainerActuallyCameUp:
    """BKP-01: `restore` used to print "[ok] Restored" unconditionally —
    `cmd_start`'s result was discarded (`cmd_start >/dev/null 2>&1 || true`)
    and nothing checked afterwards. Reproduced live: on a name with ONLY a
    volume (no container), `_exists()` (a bare `podman inspect $NAME`, no
    `--type`) matched the VOLUME's own record, `cmd_start` took the "already
    exists, just start it" branch, `podman start` failed against a
    nonexistent container (silenced), and the CLI declared success with NO
    container ever running. This class pins the postcondition check that
    now catches that class of failure regardless of which step upstream
    caused the container to never come up (modeled here via a `podman run`
    failure — the exact "no container, cmd_start must create one, creation
    fails" shape)."""

    def test_container_never_starting_is_a_clear_failure_not_a_false_ok(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        archive = _make_backup(tmp_path, fake_bin_dir, tmp_path / "backups")
        podman_log = tmp_path / "podman-restore.log"

        result = _run_safent(
            "restore", str(archive), "--force",
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home-for-restore",
            podman_log=podman_log,
            container_exists=False,  # only the volume survives the restore
            container_running=False,
            volume_exists=False,     # refusal-without-force path not exercised here
            extra_env={"FAKE_RUN_FAILS": "true"},  # cmd_start's `_run` never comes up
        )

        assert result.returncode != 0
        combined = (result.stdout + result.stderr).lower()
        assert "no safent container is running" in combined
        assert "[ok] restored" not in combined

    def test_container_that_comes_up_is_still_a_clean_success(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """The happy path (the OTHER nine tests in this file) already covers
        this, but pinned explicitly here right next to the failure case so
        the two can never silently diverge again."""
        archive = _make_backup(tmp_path, fake_bin_dir, tmp_path / "backups")
        podman_log = tmp_path / "podman-restore.log"

        result = _run_safent(
            "restore", str(archive), "--force",
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home-for-restore",
            podman_log=podman_log,
            container_exists=False,
            container_running=False,
            volume_exists=False,
        )

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "[ok] restored" in result.stdout.lower()


class TestRestoreRefusesATamperedArchive:
    def test_corrupt_state_with_matching_hash_still_refuses_before_mutation(
        self, tmp_path: Path, fake_bin_dir: Path,
    ) -> None:
        archive = _make_backup(tmp_path, fake_bin_dir, tmp_path / "backups")
        extracted = tmp_path / "broken-state"
        extracted.mkdir()
        with tarfile.open(archive) as tf:
            tf.extractall(extracted, filter="data")
        invalid_state = b"not a tar archive"
        (extracted / "state.tar").write_bytes(invalid_state)
        manifest = json.loads((extracted / "manifest.json").read_text())
        manifest["sha256"]["state.tar"] = hashlib.sha256(invalid_state).hexdigest()
        (extracted / "manifest.json").write_text(json.dumps(manifest))
        corrupt = tmp_path / "corrupt.tar.gz"
        with tarfile.open(corrupt, "w:gz") as tf:
            for name in ("manifest.json", "data-volume.tar", "state.tar"):
                tf.add(extracted / name, arcname=name)
        log = tmp_path / "restore.log"
        result = _run_safent(
            "restore", str(corrupt), "--force", fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "restore-home", podman_log=log, container_running=True,
        )
        assert result.returncode != 0
        assert not any(call.startswith(("volume rm", "volume import", "stop "))
                       for call in _podman_calls(log))

    def test_sha256_mismatch_refuses_without_touching_the_volume(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        archive = _make_backup(tmp_path, fake_bin_dir, tmp_path / "backups")

        # Corrupt data-volume.tar in place, keeping the (now stale) manifest checksum.
        extract_dir = tmp_path / "tamper"
        extract_dir.mkdir()
        with tarfile.open(archive) as tf:
            tf.extractall(extract_dir, filter="data")
        (extract_dir / "data-volume.tar").write_bytes(b"TAMPERED")
        tampered = tmp_path / "tampered.tar.gz"
        with tarfile.open(tampered, "w:gz") as tf:
            for name in ("manifest.json", "data-volume.tar", "state.tar"):
                tf.add(extract_dir / name, arcname=name)

        podman_log = tmp_path / "podman-restore.log"
        result = _run_safent(
            "restore", str(tampered), "--force",
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home-for-restore",
            podman_log=podman_log,
            container_exists=True,
            container_running=True,
            volume_exists=False,
        )

        assert result.returncode != 0
        assert "checksum" in (result.stdout + result.stderr).lower()
        calls = _podman_calls(podman_log)
        assert not any(c.startswith("volume rm") for c in calls), calls
        assert not any(c.startswith("volume import") for c in calls), calls
