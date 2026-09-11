"""`safent --porcelain` — the NDJSON wire protocol (T004, contracts/app-engine.md).

Runs the REAL `safent` CLI (POSIX sh) as a subprocess, with only `podman`
faked (a logging shim on PATH — same technique as
tests/unit/ops/test_safent_cli_backup_restore.py). `HOME` is redirected to an
isolated tmp dir so nothing ever touches the real `~/.safent`.

Covers the protocol invariants app-engine.md defines, not engine behavior:
  - stdout in porcelain mode is line-delimited JSON ONLY: every line parses,
    every line has a known `t`, every `stage` closes with exactly one `done`
    XOR one `failed` before the next stage opens (§2, §3).
  - the bootstrap ticket (`?k=...`) NEVER appears on stdout or stderr, only
    on the dedicated `--secret-fd` (§5).
  - a `failed` event's `code` maps to the documented closed vocabulary and
    the process exit code lands in the stable 10..39 domain range (§2/§3).
  - without --porcelain, the same verbs still work and print human text
    instead (§1: the flag "no altera el comportamiento").
  - `facts` is pure: it never calls a mutating podman verb (run/rm/pull/stop).
  - `SAFENT_PODMAN` wins over PATH resolution (§1 env table).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAFENT_CLI = _REPO_ROOT / "safent"

_SECRET_TOKEN = "s3cr3t-token-do-not-leak"  # noqa: S105 - test fixture, not a real credential

_KNOWN_EVENT_TYPES = {"stage", "progress", "done", "failed", "facts", "ready", "status", "url"}

_FAKE_PODMAN = """#!/usr/bin/env bash
set -e
echo "$@" >> "$FAKE_PODMAN_LOG"

case "$1" in
  inspect)
    shift
    # BKP-01: the CLI now pins `inspect --type container` so a same-named
    # volume can never satisfy a container check — accept the flag pair here.
    if [ "$1" = "--type" ]; then shift 2; fi
    if [ "$1" = "-f" ]; then
      case "$2" in
        '{{.State.Running}}')
          [ "$FAKE_CONTAINER_EXISTS" = "true" ] || exit 1
          echo "$FAKE_CONTAINER_RUNNING"; exit 0 ;;
        '{{.ImageDigest}}')
          # MAC3-02 (verificacion-mac-3.md): the REAL per-container digest
          # field — confirmed against real podman 6.1.1. The OLD fake had
          # `{{.Image}}` (the local image ID field) answer with a
          # sha256-shaped value, which is exactly the wrong assumption
          # that let the pre-fix bug (safent used `{{.Image}}` for this)
          # pass every test while failing on a real Mac.
          [ "$FAKE_CONTAINER_EXISTS" = "true" ] || exit 1
          [ "${FAKE_IMAGE_DIGEST_EMPTY:-false}" = "true" ] && exit 0
          echo "sha256:$FAKE_IMAGE_DIGEST"; exit 0 ;;
        '{{.Image}}')
          # The REAL local image ID — bare hex, no "sha256:" prefix (that
          # prefix only ever appears on a genuine digest/{{.ImageDigest}}).
          [ "$FAKE_CONTAINER_EXISTS" = "true" ] || exit 1
          echo "${FAKE_CONTAINER_IMAGE_ID:-idonlyfallback0000000000000000000000000000000000000000000000}"; exit 0 ;;
      esac
      exit 0
    fi
    [ "$FAKE_CONTAINER_EXISTS" = "true" ] && exit 0 || exit 1
    ;;
  image)
    [ "$2" = "exists" ] || exit 0
    [ "$FAKE_IMAGE_LOCAL" = "true" ] && exit 0 || exit 1
    ;;
  volume)
    case "$2" in
      exists) [ "$FAKE_VOLUME_EXISTS" = "true" ] && exit 0 || exit 1 ;;
      *) exit 0 ;;
    esac
    ;;
  port)
    echo "0.0.0.0:$FAKE_PORT"
    exit 0
    ;;
  exec)
    shift 2  # drop "exec" "$NAME"
    case "$1" in
      systemctl)
        [ "$FAKE_HEALTH_ACTIVE" = "true" ] && echo active || echo failed
        exit 0 ;;
      cat)
        [ "$FAKE_HEALTH_ACTIVE" = "true" ] && printf '%s' "$FAKE_SECRET"
        exit 0 ;;
      python3)
        printf '%s' "$FAKE_APP_VERSION"; exit 0 ;;
      *) exit 0 ;;
    esac
    ;;
  run)
    # _ensure_seccomp / _fetch_companion_file probe the image via
    # `run --rm --entrypoint cat <image> <path>` and need non-empty stdout
    # on success so they never fall through to a real network fetch.
    case " $* " in
      *" --entrypoint cat "*) printf '#!/bin/sh\\nexit 0\\n' ;;
    esac
    exit 0
    ;;
  pull)
    # MAC2-02/MAC2-03 (verificacion-mac-2.md): a real pull can sit silent
    # for over a minute between podman's own output lines — simulate that
    # shape (silent for FAKE_PULL_DELAY_SECONDS, no intermediate output at
    # all) so the heartbeat mechanism is exercised for real, not just
    # "podman printed something and we echoed it back."
    [ -n "${FAKE_PULL_DELAY_SECONDS:-}" ] && sleep "$FAKE_PULL_DELAY_SECONDS"
    [ "${FAKE_PULL_FAILS:-false}" = "true" ] && exit 1
    exit 0
    ;;
  rm|start|stop)
    exit 0
    ;;
  machine)
    shift  # drop "machine"; $1 is now list/inspect/init/start/...
    sub="$1"; shift
    case "$sub" in
      list)
        # MAC2-01 (verificacion-mac-2.md): _machines_json now also calls
        # `machine list --format json` for provider/cpus/memory (real
        # podman's `machine inspect` has none of the three) — FAKE_MACHINE_
        # DETAILS is "name:provider:cpus:memory_bytes" per line, looked up
        # per name in FAKE_MACHINES_STATE.
        if [ "${1:-}" = "--format" ] && [ "${2:-}" = "json" ]; then
          printf '['
          first=true
          if [ -n "${FAKE_MACHINES_STATE:-}" ] && [ -f "$FAKE_MACHINES_STATE" ]; then
            while IFS= read -r mname; do
              [ -n "$mname" ] || continue
              provider="unknown"; cpus=0; mem=0
              if [ -n "${FAKE_MACHINE_DETAILS:-}" ] && [ -f "$FAKE_MACHINE_DETAILS" ]; then
                line="$(grep "^$mname:" "$FAKE_MACHINE_DETAILS" 2>/dev/null | head -1)"
                if [ -n "$line" ]; then
                  provider="$(echo "$line" | cut -d: -f2)"
                  cpus="$(echo "$line" | cut -d: -f3)"
                  mem="$(echo "$line" | cut -d: -f4)"
                fi
              fi
              [ "$first" = "true" ] || printf ','
              first=false
              printf '\n    {\n        "Name": "%s",\n        "VMType": "%s",\n        "CPUs": %s,\n        "Memory": "%s"\n    }' \
                "$mname" "$provider" "$cpus" "$mem"
            done < "$FAKE_MACHINES_STATE"
          fi
          printf '\n]\n'
          exit 0
        fi
        # cmd_ensure_machine only ever calls `machine list -q`; state is a
        # plain newline-separated list of existing machine names, mutated
        # by `init` below (MAC-05, verificacion-mac-1.md tests).
        [ -n "${FAKE_MACHINES_STATE:-}" ] && [ -f "$FAKE_MACHINES_STATE" ] && cat "$FAKE_MACHINES_STATE"
        exit 0
        ;;
      inspect)
        mname="$1"; shift
        exists=false
        if [ -n "${FAKE_MACHINES_STATE:-}" ] && [ -f "$FAKE_MACHINES_STATE" ] \
           && grep -qx "$mname" "$FAKE_MACHINES_STATE"; then
          exists=true
        fi
        if [ "${1:-}" = "--format" ]; then
          [ "$exists" = "true" ] || exit 1
          case "$2" in
            '{{.Rootful}}') echo "${FAKE_MACHINE_ROOTFUL:-true}" ;;
            '{{.State}}') echo "${FAKE_MACHINE_STATE:-running}" ;;
          esac
          exit 0
        fi
        [ "$exists" = "true" ] && exit 0 || exit 1
        ;;
      init)
        [ "${FAKE_MACHINE_INIT_FAILS:-false}" = "true" ] && exit 1
        mname="$1"
        [ -n "${FAKE_MACHINES_STATE:-}" ] && echo "$mname" >> "$FAKE_MACHINES_STATE"
        exit 0
        ;;
      start)
        [ "${FAKE_MACHINE_START_FAILS:-false}" = "true" ] && exit 1
        exit 0
        ;;
      *)
        exit 0
        ;;
    esac
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


class _HealthzHandler(BaseHTTPRequestHandler):
    """MAC2-05 (verificacion-mac-2.md): `cmd_up` now curls this exact
    unauthenticated endpoint from the HOST before ever declaring ready —
    `podman`/`podman exec` are faked, but this probe is a REAL `curl`
    against a REAL socket, so tests need a real (tiny) listener, not
    another shell-script fake."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own name
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib's own signature
        pass  # silence per-request logging — tests already assert on safent's own output


@pytest.fixture()
def healthz_server():
    """Starts a real HTTP server on an OS-assigned free port, serving 200 on
    `/healthz`. Yields the port number as a str (matching `_base_env`'s own
    `port` parameter type) — callers pass it straight through."""
    server = HTTPServer(("127.0.0.1", 0), _HealthzHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield str(server.server_address[1])
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _base_env(
    *,
    fake_bin_dir: Path,
    home_dir: Path,
    podman_log: Path,
    container_exists: bool = True,
    container_running: bool = True,
    volume_exists: bool = True,
    health_active: bool = True,
    port: str = "17517",
    image_digest: str = "deadbeef",
    image_local: bool = True,
    app_version: str = "0.8.42",
    secret: str = _SECRET_TOKEN,
    pull_fails: bool = False,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    home_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin_dir}:{env.get('PATH', '')}"
    env["HOME"] = str(home_dir)
    env["SAFENT_NAME"] = "safent-test"
    env["SAFENT_DATA_VOLUME"] = "safent-test-data"
    env["FAKE_PODMAN_LOG"] = str(podman_log)
    env["FAKE_CONTAINER_EXISTS"] = "true" if container_exists else "false"
    env["FAKE_CONTAINER_RUNNING"] = "true" if container_running else "false"
    env["FAKE_VOLUME_EXISTS"] = "true" if volume_exists else "false"
    env["FAKE_HEALTH_ACTIVE"] = "true" if health_active else "false"
    env["FAKE_PORT"] = port
    env["FAKE_IMAGE_DIGEST"] = image_digest
    env["FAKE_IMAGE_LOCAL"] = "true" if image_local else "false"
    env["FAKE_APP_VERSION"] = app_version
    env["FAKE_SECRET"] = secret
    env["FAKE_PULL_FAILS"] = "true" if pull_fails else "false"
    if extra_env:
        env.update(extra_env)
    return env


def _run_safent(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
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


def _parse_ndjson(stdout: str) -> list[dict]:
    lines = [line for line in stdout.splitlines() if line]
    events = []
    for line in lines:
        parsed = json.loads(line)  # raises if any line is not valid JSON
        assert parsed.get("t") in _KNOWN_EVENT_TYPES, parsed
        events.append(parsed)
    return events


def _assert_stage_closure_invariant(events: list[dict]) -> None:
    """Every `stage` closes with exactly one `done` XOR one `failed` before
    the next stage opens; `progress` only appears within its own open stage."""
    open_stage = None
    for ev in events:
        t = ev["t"]
        if t == "stage":
            assert open_stage is None, f"stage {ev['id']!r} opened while {open_stage!r} was still open"
            open_stage = ev["id"]
        elif t == "progress":
            assert ev["id"] == open_stage, ev
        elif t in ("done", "failed"):
            assert ev["id"] == open_stage, ev
            open_stage = None
    assert open_stage is None, f"stage {open_stage!r} never closed"


def _make_bundle(
    tmp_path: Path,
    entries: list[tuple[str, bytes, str]],
    podman_version: str = "6.1.1",
    cdhashes: dict[str, str] | None = None,
) -> Path:
    """A minimal <bundle>/engine/ layout: a real copy of `safent` alongside a
    runtime-bundle.json manifest and the binaries it describes. `cdhashes`
    (MAC-02, verificacion-mac-1.md): an optional {path: cdhash} overlay —
    entries with no cdhash keep the pre-fix shape exactly (no such key at
    all), proving old-shaped manifests still work via the sha256 fallback."""
    engine_dir = tmp_path / "bundle" / "engine"
    engine_dir.mkdir(parents=True)
    (engine_dir / "safent").write_bytes(_SAFENT_CLI.read_bytes())
    (engine_dir / "safent").chmod(0o755)

    manifest_entries = []
    for rel_path, content, mode in entries:
        target = engine_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        entry = {"path": rel_path, "sha256": hashlib.sha256(content).hexdigest(), "mode": mode}
        if cdhashes and rel_path in cdhashes:
            entry["cdhash"] = cdhashes[rel_path]
        manifest_entries.append(entry)
    manifest = {"podman_version": podman_version, "entries": manifest_entries}
    (engine_dir / "runtime-bundle.json").write_text(json.dumps(manifest, indent=2))
    return engine_dir


class TestDataVolumeDerivesFromName:
    """Regression test (packaging review item 4,
    verificacion-paquete-linux.md §6): DATA_VOLUME was hardcoded to
    "safent-data" regardless of SAFENT_NAME (unlike
    ops/container/run-safent.sh's own VOLUME="${NAME}-data") — a second
    instance with its own SAFENT_NAME silently mounted the FIRST instance's
    volume if one already existed under that fixed name."""

    def test_a_custom_name_gets_its_own_derived_volume(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        env["SAFENT_NAME"] = "custom-instance"
        del env["SAFENT_DATA_VOLUME"]  # do not let the fixture's own default mask this
        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        calls = _podman_calls(podman_log)
        assert any(c == "volume exists custom-instance-data" for c in calls), calls
        assert not any("safent-data" in c for c in calls), calls

    def test_default_name_keeps_the_original_default_volume_unchanged(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        del env["SAFENT_DATA_VOLUME"]
        env.pop("SAFENT_NAME", None)
        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert any(c == "volume exists safent-data" for c in _podman_calls(podman_log))


class TestFactsIsPureObservation:
    def test_bare_facts_emits_one_line_of_parseable_json(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        lines = [line for line in result.stdout.splitlines() if line]
        assert len(lines) == 1
        facts = json.loads(lines[0])
        assert facts["os"] == "linux"
        assert isinstance(facts["freeDiskBytes"], int)
        assert facts["engineContainer"]["running"] is True
        assert facts["engineContainer"]["imageDigest"] == "sha256:deadbeef"
        assert facts["dataVolume"] is True

    def test_engine_container_image_digest_is_never_the_local_image_id(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """MAC3-02 (verificacion-mac-3.md): `inspect -f '{{.Image}}'` (the
        field the CLI used to read) returns podman's LOCAL IMAGE ID, never
        a digest — reconcile.rs's images_gap compared it against
        desired.engine_image.digest (always sha256:...) and could NEVER
        match, so a perfectly healthy, correctly-digested container was
        destroyed and recreated on every single observation. A distinct
        FAKE_CONTAINER_IMAGE_ID here proves the ID value is never what
        ends up in imageDigest — only {{.ImageDigest}}'s real digest is."""
        podman_log = tmp_path / "podman.log"
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            image_digest="realdigest0123456789",
            extra_env={"FAKE_CONTAINER_IMAGE_ID": "totallydifferentlocalimageid00000000000000000000000000000000"},
        )
        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        facts = json.loads(result.stdout.strip())
        assert facts["engineContainer"]["imageDigest"] == "sha256:realdigest0123456789"
        assert "totallydifferentlocalimageid" not in facts["engineContainer"]["imageDigest"]

    def test_engine_container_image_digest_falls_back_to_the_image_id_when_no_digest_is_recorded(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """Defensive fallback (not expected in this app's own flow — every
        container it creates runs an image pulled BY digest, so podman
        always has one recorded) for the rare case {{.ImageDigest}}
        reports nothing at all: better a comparable-but-stale ID than a
        silently absent fact."""
        podman_log = tmp_path / "podman.log"
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            extra_env={
                "FAKE_IMAGE_DIGEST_EMPTY": "true",
                "FAKE_CONTAINER_IMAGE_ID": "fallbacklocalimageid000000000000000000000000000000000000000000",
            },
        )
        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        facts = json.loads(result.stdout.strip())
        assert facts["engineContainer"]["imageDigest"] == "fallbacklocalimageid000000000000000000000000000000000000000000"

    def test_porcelain_facts_is_wrapped_in_a_facts_event(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        result = _run_safent("facts", "--porcelain", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        assert len(events) == 1
        assert events[0]["t"] == "facts"
        assert "facts" in events[0]

    def test_facts_never_calls_a_mutating_podman_verb(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        result = _run_safent("facts", "--porcelain", env=env)
        assert result.returncode == 0
        calls = _podman_calls(podman_log)
        for verb in ("run", "rm", "pull", "stop", "start"):
            assert not any(c.startswith(verb + " ") or c == verb for c in calls), calls

    def test_facts_reflects_absent_container_and_volume(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            container_exists=False,
            volume_exists=False,
        )
        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        facts = json.loads(result.stdout.strip())
        assert facts["engineContainer"]["exists"] is False
        assert facts["engineContainer"]["running"] is False
        assert facts["engineContainer"]["imageDigest"] is None
        assert facts["dataVolume"] is False

    def test_local_engine_image_digest_reflects_podman_image_exists_for_a_digest_pinned_image(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """Regression test (app-desk-integration): without this field,
        reconcile.rs's images_gap() has no way to tell "already pulled" from
        "never pulled" independent of whether a container is running it yet
        — it would ask to pull forever, even against an already-converged
        engine. `localEngineImageDigest`/`localCompanionImageDigest` close
        that gap; `podman image exists` decides them."""
        podman_log = tmp_path / "podman.log"
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            image_local=True,
            extra_env={
                "SAFENT_IMAGE": "ghcr.io/devwspito/safent@sha256:engineexample",
                "SAFENT_ADS_IMAGE": "ghcr.io/devwspito/safent-ads@sha256:adsexample",
            },
        )
        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        facts = json.loads(result.stdout.strip())
        assert facts["localEngineImageDigest"] == "sha256:engineexample"
        assert facts["localCompanionImageDigest"] == "sha256:adsexample"

    def test_local_engine_image_digest_is_null_when_not_pulled_or_not_digest_pinned(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        # Not pulled yet, even though digest-pinned.
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            image_local=False,
            extra_env={"SAFENT_IMAGE": "ghcr.io/devwspito/safent@sha256:engineexample"},
        )
        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert json.loads(result.stdout.strip())["localEngineImageDigest"] is None

        # Bare tag, not digest-pinned (plain terminal use) — never claims a match.
        env2 = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home2",
            podman_log=podman_log,
            image_local=True,
        )
        result2 = _run_safent("facts", env=env2)
        assert result2.returncode == 0
        assert json.loads(result2.stdout.strip())["localEngineImageDigest"] is None
        assert json.loads(result2.stdout.strip())["localCompanionImageDigest"] is None


class TestEnsureMachineOnLinuxIsANoOp:
    def test_ensure_machine_closes_immediately_without_touching_podman_machine(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        result = _run_safent("ensure-machine", "--porcelain", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        assert [e["t"] for e in events] == ["stage", "done"]
        assert events[0]["id"] == "machine"
        assert not any(c.startswith("machine ") for c in _podman_calls(podman_log))

    def test_non_porcelain_prints_human_text_to_stderr_not_stdout(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        result = _run_safent("ensure-machine", env=env)
        assert result.returncode == 0
        assert result.stdout == ""
        assert "[ok]" in result.stderr


_FAKE_UNAME = """#!/bin/sh
case "$1" in
  -s) echo Darwin ;;
  -m) echo arm64 ;;
  *) echo Darwin ;;
esac
"""


def _fake_darwin(fake_bin_dir: Path) -> None:
    """`cmd_ensure_machine`'s macOS branch is gated on `uname -s` (safent's
    own `OS="$(uname -s ...)"`) — faking it, not the CLI's own logic, is
    what makes these MAC-05 tests prove the REAL script's behavior on a
    simulated Mac rather than a restated assumption. Writes straight into
    the test's OWN `fake_bin_dir` (function-scoped fixture — a fresh tmp
    dir per test), so no other test's PATH is affected."""
    uname = fake_bin_dir / "uname"
    uname.write_text(_FAKE_UNAME)
    uname.chmod(0o755)


def _fake_codesign(fake_bin_dir: Path, *, verify_ok: bool, cdhash: str) -> None:
    """MAC-02 (verificacion-mac-1.md): `cmd_stage_runtime` shells out to
    `codesign --verify --strict <path>` and `codesign -dvvv <path>` for any
    manifest entry that carries a `cdhash` — faking the REAL binary (not
    the CLI's own logic) so these tests prove the actual verification
    branch, not a restated assumption. `verify_ok=False` simulates a
    tampered/invalid signature; `cdhash` is what `-dvvv` reports back.

    MAC4-01 (verificacion-mac-4.md): the REAL `codesign -d`/`-dvvv` writes
    its report to STDERR (Apple's own convention) — this fake used to
    `echo` it to stdout, which is exactly why a real-Mac-only bug
    (`safent:1920`'s `2>/dev/null` discarding that report) shipped twice
    without a single test catching it. `>&2` here makes this fake match
    the real binary's channel, so `cmd_stage_runtime`'s OWN `2>/dev/null`
    bug reproduces under test."""
    script = (
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  --verify)\n"
        f"    {'exit 0' if verify_ok else 'exit 1'} ;;\n"
        "  -dvvv)\n"
        f"    echo 'CDHash={cdhash}' >&2 ;;\n"
        "esac\n"
    )
    codesign = fake_bin_dir / "codesign"
    codesign.write_text(script)
    codesign.chmod(0o755)


def _fake_ps(fake_bin_dir: Path, output: str) -> None:
    """Fakes the REAL `ps` binary `_foreign_engine_helper` (MAC3-07) shells
    out to — `output` is exactly what `ps -axo pid=,comm=,args=` would
    print: one process per line, "<pid> <comm> <args...>". Ignores its own
    argv (the fake never needs to distinguish invocations, unlike
    `_FAKE_PODMAN`) since `_foreign_engine_helper` only ever calls `ps` one
    way."""
    ps = fake_bin_dir / "ps"
    ps.write_text(f"#!/bin/sh\ncat <<'PSEOF'\n{output}\nPSEOF\n")
    ps.chmod(0o755)


class TestEnsureMachineNeverAdoptsAForeignMachine:
    """MAC-05 (verificacion-mac-1.md): `cmd_ensure_machine` used to adopt
    whichever machine `podman machine list -q | head -1` returned first —
    on the owner's real Mac that was their OWN live `podman-machine-default`,
    started by their own separately-installed podman. This app must only
    ever create/use a machine under its OWN name and never so much as
    inspect-with-intent-to-adopt anything else."""

    def test_a_foreign_default_machine_is_never_touched_our_own_gets_created(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        _fake_darwin(fake_bin_dir)
        podman_log = tmp_path / "podman.log"
        machines_state = tmp_path / "machines.state"
        machines_state.write_text("podman-machine-default\n")
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            extra_env={"FAKE_MACHINES_STATE": str(machines_state)},
        )

        result = _run_safent("ensure-machine", "--porcelain", env=env)

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        assert events[-1]["t"] == "done"

        calls = _podman_calls(podman_log)
        assert not any("podman-machine-default" in c for c in calls), (
            f"the owner's own foreign machine must never be referenced at all: {calls}"
        )
        assert any(c == "machine init safent-test-engine --rootful --cpus 4 --memory 8192 --disk-size 60" for c in calls), (
            f"expected our OWN name (safent-test-engine, from SAFENT_NAME=safent-test) to be created: {calls}"
        )
        assert any(c.startswith("machine start safent-test-engine") for c in calls)

        machine_json = json.loads((tmp_path / "home" / ".safent" / "machine.json").read_text())
        assert machine_json == {"name": "safent-test-engine", "adopted": False}

    def test_our_own_already_existing_machine_is_reused_without_recreating(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        _fake_darwin(fake_bin_dir)
        podman_log = tmp_path / "podman.log"
        machines_state = tmp_path / "machines.state"
        # Steady state: OUR machine already exists (from a prior bootstrap)
        # alongside the owner's unrelated foreign one.
        machines_state.write_text("podman-machine-default\nsafent-test-engine\n")
        home_dir = tmp_path / "home"
        state_home = home_dir / ".safent"
        state_home.mkdir(parents=True)
        (state_home / "machine.json").write_text('{"name":"safent-test-engine","adopted":false}\n')
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=home_dir,
            podman_log=podman_log,
            extra_env={"FAKE_MACHINES_STATE": str(machines_state)},
        )

        result = _run_safent("ensure-machine", "--porcelain", env=env)

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        calls = _podman_calls(podman_log)
        assert not any(c.startswith("machine init") for c in calls), (
            f"an already-existing, already-ours machine must never be re-created: {calls}"
        )
        assert not any("podman-machine-default" in c for c in calls)
        assert any(c.startswith("machine start safent-test-engine") for c in calls)


class TestMachinesJsonReportsRealProviderAndSize:
    """MAC2-01 (verificacion-mac-2.md): `facts.machines[]` used to hardcode
    `provider:"podman"` and omit cpus/memoryBytes entirely — `MachineSpec::
    is_satisfied_by` (Rust) never matched a real machine because of it, so
    the planner treated every correctly created machine as permanent drift
    (RecreateEngine on every single boot)."""

    def test_facts_reports_real_provider_cpus_and_memory_bytes(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        _fake_darwin(fake_bin_dir)
        podman_log = tmp_path / "podman.log"
        machines_state = tmp_path / "machines.state"
        machines_state.write_text("safent-test-engine\n")
        machine_details = tmp_path / "machine.details"
        # 4 CPUs, 8192 MiB — cmd_ensure_machine's own real --cpus/--memory.
        machine_details.write_text("safent-test-engine:applehv:4:8589934592\n")
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            extra_env={
                "FAKE_MACHINES_STATE": str(machines_state),
                "FAKE_MACHINE_DETAILS": str(machine_details),
                "FAKE_MACHINE_ROOTFUL": "true",
                "FAKE_MACHINE_STATE": "running",
            },
        )

        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        machines = json.loads(result.stdout.strip())["machines"]
        assert len(machines) == 1, machines
        m = machines[0]
        assert m["name"] == "safent-test-engine"
        assert m["provider"] == "applehv", f"must be the REAL provider, not a hardcoded 'podman': {m}"
        assert m["cpus"] == 4
        assert m["memoryBytes"] == 8589934592
        assert m["rootful"] is True
        assert m["running"] is True

    def test_facts_reports_a_foreign_machine_alongside_ours_with_its_own_provider(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """The owner's own podman-machine-default (libkrun) must be
        reported honestly if it happens to be listed — never coerced into
        our own provider — even though this app never touches it
        (MAC-05/MAC2-14: separate concerns, adoption vs. observation)."""
        _fake_darwin(fake_bin_dir)
        podman_log = tmp_path / "podman.log"
        machines_state = tmp_path / "machines.state"
        machines_state.write_text("podman-machine-default\nsafent-test-engine\n")
        machine_details = tmp_path / "machine.details"
        machine_details.write_text(
            "podman-machine-default:libkrun:4:8589934592\n"
            "safent-test-engine:applehv:4:8589934592\n"
        )
        home_dir = tmp_path / "home"
        state_home = home_dir / ".safent"
        state_home.mkdir(parents=True)
        (state_home / "machine.json").write_text('{"name":"safent-test-engine","adopted":false}\n')
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=home_dir,
            podman_log=podman_log,
            extra_env={
                "FAKE_MACHINES_STATE": str(machines_state),
                "FAKE_MACHINE_DETAILS": str(machine_details),
            },
        )

        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        machines = {m["name"]: m for m in json.loads(result.stdout.strip())["machines"]}
        assert machines["podman-machine-default"]["provider"] == "libkrun"
        assert machines["podman-machine-default"]["ours"] is False
        assert machines["safent-test-engine"]["provider"] == "applehv"
        assert machines["safent-test-engine"]["ours"] is True


class TestStageRuntime:
    def test_without_a_bundle_manifest_is_a_harmless_noop(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        result = _run_safent("stage-runtime", "--porcelain", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        assert [e["t"] for e in events] == ["stage", "done"]

    def test_verified_binaries_are_staged_under_state_home_with_their_manifest_mode(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        home_dir = tmp_path / "home"
        engine_dir = _make_bundle(
            tmp_path, entries=[("podman", b"fake-podman-binary", "0755")]
        )
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=home_dir, podman_log=podman_log)
        result = subprocess.run(
            ["sh", str(engine_dir / "safent"), "stage-runtime", "--porcelain"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        assert [e["t"] for e in events] == ["stage", "progress", "done"]

        staged = home_dir / ".safent" / "runtime" / "6.1.1" / "podman"
        assert staged.read_bytes() == b"fake-podman-binary"
        assert (home_dir / ".safent" / "runtime" / "6.1.1" / ".verified").exists()

    def test_hash_mismatch_fails_closed_without_staging_anything(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        home_dir = tmp_path / "home"
        engine_dir = _make_bundle(tmp_path, entries=[("podman", b"fake-podman-binary", "0755")])
        # Corrupt the manifest's sha256 for the one entry AFTER the fact.
        manifest_path = engine_dir / "runtime-bundle.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["entries"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest, indent=2))

        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=home_dir, podman_log=podman_log)
        result = subprocess.run(
            ["sh", str(engine_dir / "safent"), "stage-runtime", "--porcelain"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 14, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        failed = events[-1]
        assert failed["t"] == "failed"
        assert failed["code"] == "runtime_hash_mismatch"
        assert failed["retryable"] is False
        assert not (home_dir / ".safent" / "runtime" / "6.1.1" / "podman").exists()

    def test_a_cdhash_entry_verifies_by_codesign_even_with_a_stale_sha256(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """MAC-02 (verificacion-mac-1.md): codesigning rewrites a Mach-O's
        bytes, so its PRE-sign sha256 recorded in the manifest never
        matches again — a `cdhash` entry must verify by signature instead
        and stage successfully despite the (deliberately, realistically)
        stale sha256 still sitting in the same manifest entry."""
        _fake_codesign(fake_bin_dir, verify_ok=True, cdhash="realcdhash0123456789abcdef")
        podman_log = tmp_path / "podman.log"
        home_dir = tmp_path / "home"
        engine_dir = _make_bundle(
            tmp_path,
            entries=[("podman", b"post-signing bytes, sha256 below is now stale", "0755")],
            cdhashes={"podman": "realcdhash0123456789abcdef"},
        )
        manifest_path = engine_dir / "runtime-bundle.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["entries"][0]["sha256"] = "0" * 64  # deliberately wrong/stale
        manifest_path.write_text(json.dumps(manifest, indent=2))

        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=home_dir, podman_log=podman_log)
        result = subprocess.run(
            ["sh", str(engine_dir / "safent"), "stage-runtime", "--porcelain"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        assert events[-1]["t"] == "done"
        staged = home_dir / ".safent" / "runtime" / "6.1.1" / "podman"
        assert staged.read_bytes() == b"post-signing bytes, sha256 below is now stale"

    def test_a_cdhash_entry_fails_closed_when_codesign_verify_rejects_it(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        _fake_codesign(fake_bin_dir, verify_ok=False, cdhash="irrelevant")
        podman_log = tmp_path / "podman.log"
        home_dir = tmp_path / "home"
        engine_dir = _make_bundle(
            tmp_path,
            entries=[("podman", b"tampered-after-signing", "0755")],
            cdhashes={"podman": "whatever-was-recorded-at-sign-time"},
        )
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=home_dir, podman_log=podman_log)
        result = subprocess.run(
            ["sh", str(engine_dir / "safent"), "stage-runtime", "--porcelain"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 14, f"stdout={result.stdout}\nstderr={result.stderr}"
        failed = _parse_ndjson(result.stdout)[-1]
        assert failed["code"] == "runtime_hash_mismatch"
        assert failed["retryable"] is False
        assert not (home_dir / ".safent" / "runtime" / "6.1.1" / "podman").exists()

    def test_a_cdhash_entry_fails_closed_when_the_real_cdhash_does_not_match(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """codesign --verify can pass (a validly signed file) while the
        SIGNATURE itself is not the one the manifest expects (e.g. resigned
        by someone else, or the wrong file entirely) — cdhash equality is
        the actual identity check, not just "is it signed at all"."""
        _fake_codesign(fake_bin_dir, verify_ok=True, cdhash="attacker-controlled-cdhash")
        podman_log = tmp_path / "podman.log"
        home_dir = tmp_path / "home"
        engine_dir = _make_bundle(
            tmp_path,
            entries=[("podman", b"some binary", "0755")],
            cdhashes={"podman": "the-real-expected-cdhash"},
        )
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=home_dir, podman_log=podman_log)
        result = subprocess.run(
            ["sh", str(engine_dir / "safent"), "stage-runtime", "--porcelain"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 14, f"stdout={result.stdout}\nstderr={result.stderr}"
        failed = _parse_ndjson(result.stdout)[-1]
        assert failed["code"] == "runtime_hash_mismatch"
        assert failed["retryable"] is False
        assert not (home_dir / ".safent" / "runtime" / "6.1.1" / "podman").exists()

    def test_an_entry_with_no_cdhash_key_still_verifies_by_sha256_unchanged(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """Backward compatibility: a manifest entry shaped exactly like
        before this pass (no `cdhash` key at all — e.g. a non-Mach-O file,
        or an older bundle) must keep working through the ORIGINAL sha256
        check, unaffected by codesign existing or not."""
        podman_log = tmp_path / "podman.log"
        home_dir = tmp_path / "home"
        engine_dir = _make_bundle(
            tmp_path, entries=[("provision.sh", b"#!/bin/sh\necho hi\n", "0755")]
        )
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=home_dir, podman_log=podman_log)
        result = subprocess.run(
            ["sh", str(engine_dir / "safent"), "stage-runtime", "--porcelain"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        staged = home_dir / ".safent" / "runtime" / "6.1.1" / "provision.sh"
        assert staged.read_bytes() == b"#!/bin/sh\necho hi\n"


class TestEnsureImages:
    def test_success_emits_pull_engine_stage(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        result = _run_safent("ensure-images", "--porcelain", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        assert events[0] == {"t": "stage", "id": "pull_engine", "label": events[0]["label"]}
        assert events[-1]["t"] == "done"
        assert any(c.startswith("pull ") for c in _podman_calls(podman_log))

    def test_registry_unreachable_fails_closed_with_the_documented_exit_code(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(
            fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log, pull_fails=True
        )
        result = _run_safent("ensure-images", "--porcelain", env=env)
        assert result.returncode == 19, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        assert events[-1]["code"] == "registry_unreachable"
        assert events[-1]["retryable"] is True

    def test_a_silent_multi_second_pull_still_emits_progress_heartbeats(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """MAC2-02/MAC2-03 (verificacion-mac-2.md): a real pull_engine
        measured 84 s with a 60.9 s window with NOT ONE line on any
        channel — app-engine.md §3.2 requires progress at least every 5 s
        while a stage is alive, and the adapter's 15 s stall watchdog
        killed the CLI well before that. `podman pull` here is silent for
        6 s straight (no intermediate output at all, the worst case) —
        the CLI itself must still emit progress on its own cadence."""
        podman_log = tmp_path / "podman.log"
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            extra_env={"FAKE_PULL_DELAY_SECONDS": "6"},
        )
        result = _run_safent("ensure-images", "--porcelain", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        progress_events = [e for e in events if e["t"] == "progress" and e["id"] == "pull_engine"]
        assert len(progress_events) >= 1, (
            f"a 6 s silent pull must still emit at least one heartbeat: {events}"
        )
        for e in progress_events:
            assert e["unit"] == "steps"
            assert "total" not in e, "the heartbeat's total is genuinely unknown, must be omitted, not guessed"
        assert events[-1]["t"] == "done"


def _run_up_with_secret_pipe(
    *args: str, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run `up` with a real pipe open for the ticket descriptor, passed to
    the child at whatever fd number the OS actually gave it — forcing a
    SPECIFIC number (e.g. 3) via preexec_fn is unreliable with CPython's
    subprocess (close_fds runs AFTER preexec_fn, closing anything not in the
    ORIGINAL pass_fds set). --secret-fd is designed to be told the number
    instead, which is exactly what a real embedding wrapper would do too.

    Runs inside capsys.disabled(): pytest's default fd-level capture holds
    its own low-numbered descriptors open, which corrupts fd inheritance for
    a child that expects a specific extra fd — real terminal fds are needed
    here, same as any other test driving raw fd plumbing under pytest.
    Returns (result, ticket_text)."""
    r_fd, w_fd = os.pipe()
    os.set_inheritable(w_fd, True)
    try:
        with capsys.disabled():
            result = subprocess.run(
                ["sh", str(_SAFENT_CLI), "--no-companion", "up", *args, "--secret-fd", str(w_fd)],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                pass_fds=(w_fd,),
            )
    finally:
        os.close(w_fd)
    ticket = os.read(r_fd, 65536).decode()
    os.close(r_fd)
    return result, ticket


class TestUpDeliversTheTicketOnlyOnTheSecretFd:
    def test_porcelain_up_never_leaks_the_ticket_on_stdout_or_stderr(
        self, tmp_path: Path, fake_bin_dir: Path, healthz_server: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(
            fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log, port=healthz_server
        )

        result, ticket = _run_up_with_secret_pipe("--porcelain", env=env, capsys=capsys)

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert ticket.strip() == f"http://127.0.0.1:{healthz_server}/?k={_SECRET_TOKEN}"
        assert _SECRET_TOKEN not in result.stdout
        assert _SECRET_TOKEN not in result.stderr

        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        assert [e["t"] for e in events] == ["stage", "done", "stage", "done", "ready"]
        assert events[-1] == {"t": "ready", "endpoint_ref": "stdout-secret"}

    def test_non_porcelain_up_prints_the_url_to_stdout_like_the_existing_url_verb(
        self, tmp_path: Path, fake_bin_dir: Path, healthz_server: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(
            fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log, port=healthz_server
        )

        result, ticket = _run_up_with_secret_pipe(env=env, capsys=capsys)

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert result.stdout.strip() == f"http://127.0.0.1:{healthz_server}/?k={_SECRET_TOKEN}"
        assert ticket == ""  # non-porcelain `up` never touches --secret-fd

    def test_daemon_never_becoming_active_fails_closed(
        self, tmp_path: Path, fake_bin_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            health_active=False,
        )
        result, ticket = _run_up_with_secret_pipe("--porcelain", env=env, capsys=capsys)

        assert result.returncode == 24, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert ticket == ""
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        assert events[-1]["t"] == "failed"
        assert events[-1]["code"] == "daemon_unhealthy"
        assert _SECRET_TOKEN not in result.stdout
        assert _SECRET_TOKEN not in result.stderr

    def test_ready_never_fires_if_the_published_port_never_answers_from_the_host(
        self, tmp_path: Path, fake_bin_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """MAC2-05 (verificacion-mac-2.md): a real Mac run had systemd
        report the daemon active, the bootstrap secret readable — and
        `ready` fired — while the published port answered `000` from the
        HOST (only `307` from the VM's own loopback). No server at all is
        listening here — the exact "engine healthy inside, unreachable
        outside" shape, on a port fast/cheap enough for a unit test to
        actually exhaust the retry budget (SAFENT_HEALTHZ_PROBE_ATTEMPTS)."""
        podman_log = tmp_path / "podman.log"
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            port="1",  # nothing ever listens on port 1 without root — always refused
            extra_env={
                "SAFENT_HEALTHZ_PROBE_ATTEMPTS": "2",
                "SAFENT_HEALTHZ_PROBE_INTERVAL_SECONDS": "1",
            },
        )
        result, ticket = _run_up_with_secret_pipe("--porcelain", env=env, capsys=capsys)

        assert result.returncode == 24, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert ticket == "", "no ticket must ever reach the secret fd for a port nobody answers on"
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        assert events[-1]["t"] == "failed"
        assert events[-1]["code"] == "daemon_unhealthy"
        assert "ready" not in [e["t"] for e in events], (
            "ready must NEVER fire on internal-only evidence — this is the whole point of MAC2-05"
        )

    def test_probe_retries_until_the_port_actually_starts_answering(
        self, tmp_path: Path, fake_bin_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The systemd unit can report active a moment before the app
        itself is actually accepting connections — the probe must retry,
        not fail on the very first attempt. The socket is not even BOUND
        until after the delay (an already-bound-but-not-yet-served
        HTTPServer still completes the TCP handshake instantly, which
        would make a single curl attempt succeed without ever retrying —
        this must reproduce "connection refused", not "slow response")."""
        import socket
        import time

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = str(probe.getsockname()[1])
        probe.close()

        holder: dict[str, HTTPServer] = {}

        def _start_late() -> None:
            time.sleep(1.5)
            server = HTTPServer(("127.0.0.1", int(port)), _HealthzHandler)
            holder["server"] = server
            server.serve_forever()

        thread = threading.Thread(target=_start_late, daemon=True)
        thread.start()
        try:
            podman_log = tmp_path / "podman.log"
            env = _base_env(
                fake_bin_dir=fake_bin_dir,
                home_dir=tmp_path / "home",
                podman_log=podman_log,
                port=port,
                extra_env={
                    "SAFENT_HEALTHZ_PROBE_ATTEMPTS": "10",
                    "SAFENT_HEALTHZ_PROBE_INTERVAL_SECONDS": "1",
                },
            )
            result, ticket = _run_up_with_secret_pipe("--porcelain", env=env, capsys=capsys)

            assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
            assert ticket.strip() == f"http://127.0.0.1:{port}/?k={_SECRET_TOKEN}"
            events = _parse_ndjson(result.stdout)
            _assert_stage_closure_invariant(events)
            assert events[-1] == {"t": "ready", "endpoint_ref": "stdout-secret"}
            progress_events = [e for e in events if e["t"] == "progress"]
            assert len(progress_events) >= 1, "at least one retry must have happened before success"
        finally:
            if "server" in holder:
                holder["server"].shutdown()
            thread.join(timeout=5)


class TestUpConvergesInsteadOfDestroyingAHealthyEngine:
    """MAC4-02 (verificacion-mac-4.md, third distinct cause): `boot.rs`'s
    `confirm_ready` re-invokes `up` on EVERY reopen with an already-healthy
    engine, just to re-mint the ticket — `_run` used to `rm -f` and
    recreate UNCONDITIONALLY, so a perfectly healthy, digest-matching
    container lost its id and port on every single reopen (measured live:
    12.7 s, new id, new port). `up` must reuse a converged container
    (exists, running, serving the DESIRED digest) instead of destroying
    it — only when SAFENT_IMAGE is digest-pinned (repo@sha256:...), the
    only shape the desktop app ever sets."""

    def test_reopening_twice_never_destroys_or_recreates_a_converged_container(
        self, tmp_path: Path, fake_bin_dir: Path, healthz_server: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        podman_log = tmp_path / "podman.log"
        digest = "deadbeef" * 8  # 64 hex chars — sha256-shaped, value itself is arbitrary
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            port=healthz_server,
            image_digest=digest,
            extra_env={"SAFENT_IMAGE": f"ghcr.io/devwspito/safent@sha256:{digest}"},
        )

        result1, ticket1 = _run_up_with_secret_pipe("--porcelain", env=env, capsys=capsys)
        assert result1.returncode == 0, f"stdout={result1.stdout}\nstderr={result1.stderr}"
        calls_after_first = _podman_calls(podman_log)
        assert not any(c.startswith("rm -f ") for c in calls_after_first), calls_after_first
        assert not any(c.startswith("run -d ") for c in calls_after_first), calls_after_first

        result2, ticket2 = _run_up_with_secret_pipe("--porcelain", env=env, capsys=capsys)
        assert result2.returncode == 0, f"stdout={result2.stdout}\nstderr={result2.stderr}"
        calls_after_both = _podman_calls(podman_log)
        calls_from_second_run = calls_after_both[len(calls_after_first) :]
        assert not any(c.startswith("rm -f ") for c in calls_from_second_run), calls_from_second_run
        assert not any(c.startswith("run -d ") for c in calls_from_second_run), calls_from_second_run

        assert ticket1.strip() == ticket2.strip() == f"http://127.0.0.1:{healthz_server}/?k={_SECRET_TOKEN}"

    def test_a_non_digest_pinned_image_still_recreates_every_time_unchanged(
        self, tmp_path: Path, fake_bin_dir: Path, healthz_server: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bare terminal install (SAFENT_IMAGE unset, or a plain tag) has
        no digest to converge against — it must keep today's behavior
        (always recreate) rather than silently claiming convergence."""
        podman_log = tmp_path / "podman.log"
        env = _base_env(
            fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log, port=healthz_server
        )

        result, ticket = _run_up_with_secret_pipe("--porcelain", env=env, capsys=capsys)

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        calls = _podman_calls(podman_log)
        assert any(c.startswith("rm -f ") for c in calls), calls
        assert any(c.startswith("run -d ") for c in calls), calls


class TestStatusHonoursPorcelain:
    """CLI-N2 (specs/025-safent-repaso matriz-final-39eeb8e): `cmd_status`
    (safent:788-796 at the time of the finding) `echo`d human text
    unconditionally, even under --porcelain — the only channel invariant
    app-engine.md §2 states ("stdout — exclusivamente NDJSON... Nada más se
    escribe aquí"). Not in the closed verb table of §4 either, so this pins
    the CLI's own general porcelain contract, not a wrapper dependency."""

    def test_non_porcelain_running_is_unchanged_human_text_on_stdout(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        result = _run_safent("status", env=env)
        assert result.returncode == 0
        assert result.stdout.strip() == "[ok] Safent running at  http://localhost:17517/   (open with: safent)"
        assert result.stderr == ""

    def test_porcelain_running_emits_ndjson_status_event_only(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        result = _run_safent("status", "--porcelain", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        assert events == [{"t": "status", "state": "running", "port": 17517}]
        assert "[ok]" not in result.stdout
        assert "http://localhost" not in result.stdout

    def test_porcelain_stopped_and_not_installed_states(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        stopped_env = _base_env(
            fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home-stopped", podman_log=podman_log,
            container_exists=True, container_running=False,
        )
        stopped = _run_safent("status", "--porcelain", env=stopped_env)
        assert stopped.returncode == 0, f"stdout={stopped.stdout}\nstderr={stopped.stderr}"
        assert _parse_ndjson(stopped.stdout) == [{"t": "status", "state": "stopped"}]

        absent_env = _base_env(
            fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home-absent", podman_log=podman_log,
            container_exists=False, container_running=False,
        )
        absent = _run_safent("status", "--porcelain", env=absent_env)
        assert absent.returncode == 0, f"stdout={absent.stdout}\nstderr={absent.stderr}"
        assert _parse_ndjson(absent.stdout) == [{"t": "status", "state": "not_installed"}]


class TestUrlHonoursPorcelain:
    """CLI-N2: `cmd_url` (safent:379-383 at the time of the finding) printed
    the bootstrap URL — WITH the `?k=` ticket — to stdout even under
    --porcelain, against app-engine.md §5 ("Nunca en stdout"). Non-porcelain
    behaviour (this command's whole purpose: hand the URL to the caller) is
    unchanged; under --porcelain the same line moves to stderr and stdout
    gets a secret-free completion marker instead."""

    def test_non_porcelain_prints_the_url_with_ticket_to_stdout_unchanged(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        result = _run_safent("url", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert result.stdout.strip() == f"http://localhost:17517/?k={_SECRET_TOKEN}"

    def test_porcelain_never_leaks_the_ticket_on_stdout(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        result = _run_safent("url", "--porcelain", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert _SECRET_TOKEN not in result.stdout
        assert "?k=" not in result.stdout

        events = _parse_ndjson(result.stdout)
        assert events == [{"t": "url", "delivered_via": "stderr"}]

        # The URL still reaches the caller — just no longer via stdout.
        assert f"http://localhost:17517/?k={_SECRET_TOKEN}" in result.stderr


class TestSafentPodmanOverridesPath:
    def test_facts_uses_safent_podman_even_when_path_podman_would_fail(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        # PATH's `podman` fails hard on ANY invocation — proves it was never called.
        broken = fake_bin_dir / "podman"
        broken.write_text("#!/usr/bin/env bash\nexit 97\n")
        broken.chmod(0o755)

        pinned_dir = tmp_path / "pinned"
        pinned_dir.mkdir()
        pinned = pinned_dir / "podman"
        pinned.write_text(_FAKE_PODMAN)
        pinned.chmod(0o755)

        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        env["SAFENT_PODMAN"] = str(pinned)

        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        facts = json.loads(result.stdout.strip())
        assert facts["engineContainer"]["running"] is True


class TestBundledPodmanGetsItsOwnStorage:
    """Regression test (packaging review item 3,
    specs/028-safent-app-nativa/verificacion-paquete-linux.md §"Pasada 1"):
    a bundled STATIC (musl) podman and the host's own (glibc) podman/docker
    collide on ONE shared per-uid /dev/shm lock segment ("failed to open
    2048 locks ... numerical result out of range"). safent must give the
    PINNED podman (SAFENT_PODMAN set — never a bare terminal `podman`) its
    own storage tree under SAFENT_STATE_HOME and its own containers.conf,
    without ever touching the host's default ~/.local/share/containers."""

    def test_pinned_podman_gets_a_private_storage_conf_and_containers_conf(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        pinned_dir = tmp_path / "bundle"
        pinned_dir.mkdir()
        pinned = pinned_dir / "podman"
        pinned.write_text(_FAKE_PODMAN)
        pinned.chmod(0o755)
        # The file stage-runtime.sh's _patch_bundled_containers_conf produces,
        # sitting beside the pinned podman exactly as it would once flattened.
        (pinned_dir / "containers.conf").write_text(
            '[engine]\ncgroup_manager = "cgroupfs"\nlock_type = "file"\n'
        )

        home_dir = tmp_path / "home"
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=home_dir, podman_log=podman_log)
        env["SAFENT_PODMAN"] = str(pinned)
        state_home = home_dir / ".safent"
        env["SAFENT_STATE_HOME"] = str(state_home)

        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

        storage_conf = state_home / "podman" / "storage.conf"
        assert storage_conf.is_file(), "safent must generate its own storage.conf"
        content = storage_conf.read_text()
        assert str(state_home / "podman" / "storage") in content
        assert str(state_home / "podman" / "runroot") in content
        # Never the host's own default rootless storage path.
        assert ".local/share/containers" not in content

    def test_a_bare_terminal_podman_on_path_is_left_completely_alone(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """SAFENT_PODMAN unset (a dev/owner's own podman/docker on PATH) must
        NEVER be redirected to a private storage tree — that would silently
        orphan whatever they already have there."""
        home_dir = tmp_path / "home"
        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=home_dir, podman_log=podman_log)
        state_home = home_dir / ".safent"
        env["SAFENT_STATE_HOME"] = str(state_home)
        env.pop("SAFENT_PODMAN", None)

        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert not (state_home / "podman" / "storage.conf").exists()


class TestSeccompProfileResolvesFromTheBundleNotTheStateDir:
    """MAC3-03 (verificacion-mac-3.md, MAC2-07 repeated unfixed): a real Mac
    run failed with a raw podman error — "opening seccomp profile failed:
    open <path>: no such file or directory" — because the profile was
    fetched INTO $SAFENT_STATE_HOME at runtime (image extraction or a
    raw.githubusercontent download), and podman actually runs INSIDE the
    podman-machine VM, which only ever virtiofs-mounts /Users, /private and
    /var/folders. Shipping the profile as a bundled, hash-verified runtime
    asset (like podman/gvproxy/vfkit) removes the network/image dependency
    entirely for the PINNED-podman (desktop app) case."""

    def test_bundled_profile_is_used_instead_of_fetching_at_runtime(
        self, tmp_path: Path, fake_bin_dir: Path, healthz_server: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pinned_dir = tmp_path / "bundle"
        pinned_dir.mkdir()
        pinned = pinned_dir / "podman"
        pinned.write_text(_FAKE_PODMAN)
        pinned.chmod(0o755)
        # ops/container/seccomp/safent.json, staged flat exactly as
        # stage-runtime.sh's APP_FILES would (see runtime-manifest.lock's
        # app_files.entries) — content is irrelevant to this test, only
        # its PATH being the one actually used matters.
        bundled_profile = pinned_dir / "safent.json"
        bundled_profile.write_text('{"defaultAction":"SCMP_ACT_ERRNO"}')

        podman_log = tmp_path / "podman.log"
        env = _base_env(
            fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log, port=healthz_server
        )
        env["SAFENT_PODMAN"] = str(pinned)

        result, ticket = _run_up_with_secret_pipe("--porcelain", env=env, capsys=capsys)

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert ticket.strip() == f"http://127.0.0.1:{healthz_server}/?k={_SECRET_TOKEN}"
        run_calls = [c for c in _podman_calls(podman_log) if c.startswith("run -d ")]
        assert len(run_calls) == 1, run_calls
        assert f"--security-opt seccomp={bundled_profile}" in run_calls[0], run_calls[0]

    def test_state_home_given_as_a_symlink_is_canonicalized_before_use(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """Reproduces the exact shape of the real failure: verificacion-mac-3.md's
        own harness set SAFENT_STATE_HOME to a /tmp path, which is itself a
        symlink to /private/tmp on macOS — the HOST resolves it fine, but
        the guest VM (a different OS, no such symlink) cannot. The report's
        own pass 2 proved the CANONICAL form of the identical folder works;
        this asserts safent now canonicalizes any override itself instead
        of depending on the caller already spelling it that way."""
        real_state = tmp_path / "real-state"
        real_state.mkdir()
        alias_state = tmp_path / "alias-state"
        alias_state.symlink_to(real_state)

        pinned_dir = tmp_path / "bundle"
        pinned_dir.mkdir()
        pinned = pinned_dir / "podman"
        pinned.write_text(_FAKE_PODMAN)
        pinned.chmod(0o755)

        podman_log = tmp_path / "podman.log"
        env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=tmp_path / "home", podman_log=podman_log)
        env["SAFENT_PODMAN"] = str(pinned)
        env["SAFENT_STATE_HOME"] = str(alias_state)

        result = _run_safent("facts", env=env)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

        storage_conf = real_state / "podman" / "storage.conf"
        assert storage_conf.is_file(), "safent must generate its own storage.conf under the CANONICAL state home"
        content = storage_conf.read_text()
        assert str(real_state / "podman" / "storage") in content
        assert str(alias_state) not in content, (
            f"storage.conf must record the canonical path, not the symlink alias: {content}"
        )


class TestMachineInitUsesTheBundledImage:
    """MAC-06 (verificacion-mac-1.md): `machine init` shipped with no
    `--image` at all, so the bundled 932 MB `podman-machine.aarch64.
    applehv.raw.zst` (93% of the DMG) went unused and podman would download
    its own copy from quay.io — contradicting contracts/app-engine.md §4's
    "crea la nuestra desde la imagen empaquetada, sin red". Only applies to
    the PINNED podman (SAFENT_PODMAN set, same distinction
    TestBundledPodmanGetsItsOwnStorage draws) — a bare terminal install
    ships no machine image to point at."""

    def test_machine_init_receives_image_and_provider_when_the_bundled_image_is_present(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        _fake_darwin(fake_bin_dir)
        pinned_dir = tmp_path / "bundle"
        pinned_dir.mkdir()
        pinned = pinned_dir / "podman"
        pinned.write_text(_FAKE_PODMAN)
        pinned.chmod(0o755)
        image_file = pinned_dir / "podman-machine.aarch64.applehv.raw.zst"
        image_file.write_bytes(b"not a real disk image, existence is what's tested")

        podman_log = tmp_path / "podman.log"
        machines_state = tmp_path / "machines.state"
        machines_state.write_text("")
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            extra_env={"FAKE_MACHINES_STATE": str(machines_state)},
        )
        env["SAFENT_PODMAN"] = str(pinned)

        result = _run_safent("ensure-machine", "--porcelain", env=env)

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        calls = _podman_calls(podman_log)
        expected = (
            f"machine init safent-test-engine --image {image_file} --provider applehv "
            "--rootful --cpus 4 --memory 8192 --disk-size 60"
        )
        assert expected in calls, f"expected exactly: {expected!r}\ngot: {calls}"

    def test_machine_init_omits_image_and_provider_when_the_bundled_image_file_is_absent(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        _fake_darwin(fake_bin_dir)
        pinned_dir = tmp_path / "bundle"
        pinned_dir.mkdir()
        pinned = pinned_dir / "podman"
        pinned.write_text(_FAKE_PODMAN)
        pinned.chmod(0o755)
        # Deliberately NOT creating podman-machine.aarch64.applehv.raw.zst.

        podman_log = tmp_path / "podman.log"
        machines_state = tmp_path / "machines.state"
        machines_state.write_text("")
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            extra_env={"FAKE_MACHINES_STATE": str(machines_state)},
        )
        env["SAFENT_PODMAN"] = str(pinned)

        result = _run_safent("ensure-machine", "--porcelain", env=env)

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        calls = _podman_calls(podman_log)
        assert "machine init safent-test-engine --rootful --cpus 4 --memory 8192 --disk-size 60" in calls
        assert not any("--image" in c for c in calls), calls


class TestEnsureMachineFailsLoudlyOnAForeignHelperBinary:
    """MAC3-07 (verificacion-mac-3.md, MAC-07/MAC2-13 repeated unfixed): a
    real Mac had its machine started fine, but `ps` showed the OWNER's own
    `/opt/podman/bin/gvproxy`/`vfkit` actually serving it (different
    sha256) — no bundled containers.conf steered podman's helper
    resolution. `cmd_ensure_machine` must now catch this and fail loudly
    instead of shipping silently."""

    def test_a_foreign_gvproxy_serving_our_own_machine_fails_the_machine_stage(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        _fake_darwin(fake_bin_dir)
        pinned_dir = tmp_path / "bundle"
        pinned_dir.mkdir()
        pinned = pinned_dir / "podman"
        pinned.write_text(_FAKE_PODMAN)
        pinned.chmod(0o755)
        (pinned_dir / "bin").mkdir()
        (pinned_dir / "bin" / "gvproxy").write_bytes(b"bundled gvproxy")
        (pinned_dir / "bin" / "vfkit").write_bytes(b"bundled vfkit")
        # The OWNER's own podman.io install — a DIFFERENT path — is what is
        # actually running for OUR machine (safent-test-engine).
        _fake_ps(
            fake_bin_dir,
            "84104 /opt/podman/bin/gvproxy --listen safent-test-engine\n"
            "84106 /opt/podman/bin/vfkit --machine safent-test-engine",
        )

        podman_log = tmp_path / "podman.log"
        machines_state = tmp_path / "machines.state"
        machines_state.write_text("")
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            extra_env={"FAKE_MACHINES_STATE": str(machines_state)},
        )
        env["SAFENT_PODMAN"] = str(pinned)

        result = _run_safent("ensure-machine", "--porcelain", env=env)

        assert result.returncode == 16, f"stdout={result.stdout}\nstderr={result.stderr}"  # machine_start_failed
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        failed = events[-1]
        assert failed["t"] == "failed"
        assert failed["code"] == "machine_start_failed"
        assert "foreign helper binary" in failed["detail"]
        assert "/opt/podman/bin/gvproxy" in failed["detail"]
        assert failed["retryable"] is False

    def test_the_bundled_gvproxy_serving_our_own_machine_passes(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        _fake_darwin(fake_bin_dir)
        pinned_dir = tmp_path / "bundle"
        pinned_dir.mkdir()
        pinned = pinned_dir / "podman"
        pinned.write_text(_FAKE_PODMAN)
        pinned.chmod(0o755)
        (pinned_dir / "bin").mkdir()
        (pinned_dir / "bin" / "gvproxy").write_bytes(b"bundled gvproxy")
        (pinned_dir / "bin" / "vfkit").write_bytes(b"bundled vfkit")
        # The comm path matches the BUNDLED one exactly — no problem.
        _fake_ps(
            fake_bin_dir,
            f"84104 {pinned_dir}/bin/gvproxy --listen safent-test-engine\n"
            f"84106 {pinned_dir}/bin/vfkit --machine safent-test-engine",
        )

        podman_log = tmp_path / "podman.log"
        machines_state = tmp_path / "machines.state"
        machines_state.write_text("")
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            extra_env={"FAKE_MACHINES_STATE": str(machines_state)},
        )
        env["SAFENT_PODMAN"] = str(pinned)

        result = _run_safent("ensure-machine", "--porcelain", env=env)

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        _assert_stage_closure_invariant(events)
        assert events[-1]["t"] == "done"

    def test_no_gvproxy_or_vfkit_running_yet_is_not_a_failure(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """A machine that just started and has not spun up its helpers yet
        (or a `ps` that finds nothing at all) must never be treated as a
        foreign-helper failure — absence of evidence is not evidence."""
        _fake_darwin(fake_bin_dir)
        pinned_dir = tmp_path / "bundle"
        pinned_dir.mkdir()
        pinned = pinned_dir / "podman"
        pinned.write_text(_FAKE_PODMAN)
        pinned.chmod(0o755)
        (pinned_dir / "bin").mkdir()
        (pinned_dir / "bin" / "gvproxy").write_bytes(b"bundled gvproxy")
        (pinned_dir / "bin" / "vfkit").write_bytes(b"bundled vfkit")
        _fake_ps(fake_bin_dir, "1 /sbin/launchd\n2 /usr/libexec/something-unrelated")

        podman_log = tmp_path / "podman.log"
        machines_state = tmp_path / "machines.state"
        machines_state.write_text("")
        env = _base_env(
            fake_bin_dir=fake_bin_dir,
            home_dir=tmp_path / "home",
            podman_log=podman_log,
            extra_env={"FAKE_MACHINES_STATE": str(machines_state)},
        )
        env["SAFENT_PODMAN"] = str(pinned)

        result = _run_safent("ensure-machine", "--porcelain", env=env)

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        events = _parse_ndjson(result.stdout)
        assert events[-1]["t"] == "done"
