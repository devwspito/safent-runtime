"""safent companion scaffolding is ALWAYS present at Safent start (028 T015,
029 CL-002) — even when the companion service itself is never brought up.

Before this: `_provision_companion` ran the FULL provision.sh (network + TLS
+ bearer + companions.json + image pull + secrets + SSO keygen + compose up
+ health wait) as a side effect of every `safent start`/`open`/`update`. A
failed or skipped image pull left `COMPANION_RUN_ARGS` empty, and Safent's
OWN container was created WITHOUT the companion's `--network`/bind mounts —
so installing the companion later required recreating Safent's container
(you cannot add a bind mount to a container that already exists).

Now `_provision_companion` calls `provision.sh --scaffold`: local,
image-independent, always fast. Safent's container is ALWAYS created with
the companion's network + all four read-only binds, whether or not the
companion's own service (compose stack) is up — `safent companion install`
(T016) only ever writes into mounts that already exist.

Drives the REAL `safent` script (sh, not sourced) with only podman faked —
same discipline as tests/unit/ops/test_companion_provision.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAFENT_CLI = _REPO_ROOT / "safent"
_PROVISION_SH = _REPO_ROOT / "ops/container/companions/ads/provision.sh"
_COMPOSE_YAML = _REPO_ROOT / "ops/container/companions/ads/compose.yaml"
_CAPS_TEMPLATE = _REPO_ROOT / "ops/container/companions/ads/caps.template.yaml"

# `run -d --name safent ...` is what `_run()` actually invokes to create
# Safent's own container — every other verb here is a companion-scaffold
# primitive (network inspect/create) that must succeed WITHOUT ever pulling
# or running the ads image (that is exactly what --scaffold must never do).
_FAKE_PODMAN = """#!/usr/bin/env bash
set -e
echo "$@" >> "$FAKE_PODMAN_LOG"
case "$1" in
  inspect)
    # `_exists`: no container yet -> cmd_start takes the create path.
    exit 1
    ;;
  network)
    case "$2" in
      inspect) [ "${FAKE_NETWORK_PRESENT:-0}" = "1" ] && exit 0 || exit 1 ;;
      create) exit 0 ;;
    esac
    exit 0
    ;;
  pull|rm)
    exit 0
    ;;
  run)
    # `run -d --name ...` is `_run()` actually CREATING Safent's own
    # container — must succeed. `run --rm --entrypoint cat ...` is
    # `_ensure_seccomp`'s / `_fetch_companion_file`'s image-baked-file
    # probe — always fail it so both fall back to their cache (pre-seeded
    # below), never to the network (this test has none faked).
    case " $* " in
      *" -d "*) exit 0 ;;
      *" safent-companion-runtime:/runtime "*) cat >/dev/null; exit 0 ;;
      *) exit 1 ;;
    esac
    ;;
esac
exit 0
"""


# `_fetch_companion_file`'s 2nd tier (curl from raw.githubusercontent.com)
# must never be REACHABLE in this test — a sandbox with real network access
# would silently overwrite the pre-seeded (edited, --scaffold-capable)
# provision.sh with whatever is on the default branch upstream, hiding the
# very behaviour this test exists to prove. Force it to the 3rd tier
# (already-cached file) deterministically instead of relying on the
# sandbox happening to be offline.
_FAKE_CURL = """#!/usr/bin/env bash
exit 1
"""


@pytest.fixture()
def fake_bin_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text(_FAKE_PODMAN)
    podman.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(_FAKE_CURL)
    curl.chmod(0o755)
    return bin_dir


def _seed_state_home(state_home: Path) -> None:
    """Pre-seed everything `_ensure_seccomp` / `_fetch_companion_file` would
    otherwise need network/image access for, so this test exercises ONLY
    the scaffold-vs-full decision, not those already-covered fallbacks."""
    state_home.mkdir(parents=True, exist_ok=True)
    (state_home / "safent-seccomp.json").write_text("{}")
    bin_dir = state_home / "companions" / "ads" / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy(_PROVISION_SH, bin_dir / "provision.sh")
    (bin_dir / "provision.sh").chmod(0o755)
    shutil.copy(_COMPOSE_YAML, bin_dir / "compose.yaml")
    shutil.copy(_CAPS_TEMPLATE, bin_dir / "caps.template.yaml")


def _run_safent_start(
    tmp_path: Path, fake_bin_dir: Path, *, network_present: bool = False
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    state_home = tmp_path / "state-home"
    _seed_state_home(state_home)
    podman_log = tmp_path / "podman.log"
    env = {
        **os.environ,
        "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "home"),
        "SAFENT_STATE_HOME": str(state_home),
        "SAFENT_NAME": "scaffold-test",
        "FAKE_PODMAN_LOG": str(podman_log),
        "FAKE_NETWORK_PRESENT": "1" if network_present else "0",
    }
    result = subprocess.run(
        ["sh", str(_SAFENT_CLI), "start"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    return result, podman_log, state_home


class TestScaffoldAlwaysRunsOnStart:
    def test_run_command_includes_only_the_private_linux_projection(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        result, podman_log, _ = _run_safent_start(tmp_path, fake_bin_dir)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        log_lines = podman_log.read_text().splitlines()
        run_lines = [ln for ln in log_lines if ln.startswith("run -d --name")]
        assert len(run_lines) == 1, log_lines
        run_line = run_lines[0]
        assert "--network safent-companions" in run_line
        assert "safent-companion-runtime:/etc/hermes/companions:ro" in run_line
        assert "/companions/ads/bearer:" not in run_line
        assert "/companions/ads/sso/ads-sso.key:" not in run_line

    def test_scaffold_never_pulls_or_runs_the_ads_image(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """The regression this test guards: before T015, EVERY `safent
        start` ran the FULL provision.sh (image pull + compose up) as a
        side effect. Scaffold mode must never touch the ads image at all."""
        result, podman_log, _ = _run_safent_start(tmp_path, fake_bin_dir)
        assert result.returncode == 0, result.stderr
        log_lines = podman_log.read_text().splitlines()
        assert not any(ln.startswith("compose ") for ln in log_lines), log_lines
        assert not any("gen_keys" in ln for ln in log_lines), log_lines

    def test_scaffold_files_exist_before_any_companion_install(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        result, _, state_home = _run_safent_start(tmp_path, fake_bin_dir)
        assert result.returncode == 0, result.stderr
        companion_state = state_home / "companions" / "ads"
        assert (companion_state / "companions.json").exists()
        assert (companion_state / "bearer").exists()
        assert (companion_state / "tls" / "ca.crt").exists()
        assert (companion_state / "sso" / "ads-sso.key").exists()
        # The SSO key is a deferred placeholder until `safent companion
        # install` — everything ELSE is real from the very first boot.
        assert (companion_state / "sso" / "ads-sso.key").read_bytes() == b""
        assert (companion_state / "bearer").read_text().strip() != ""
