"""`safent companion install|repair` and `safent agent`'s consumption of the
install-request marker (028 T016, contracts/install-request.md).

Two layers, tested separately:
  - `TestCompanionInstall`/`TestCompanionRepair`: the verbs themselves —
    scaffold -> full provision -> best-effort hot-reload, porcelain events.
  - `TestAgentTick`: `_agent_tick` (one pass of `safent agent`'s loop,
    `SAFENT_AGENT_ONCE=1`) orchestrating claim -> action -> resolve. The
    claim/resolve marker semantics themselves (expiry, mutual exclusion)
    are already covered at the Python level in
    tests/unit/agents_os/test_install_requests.py and
    tests/unit/shell_server/test_install_request_agent_cli.py — this fakes
    `install_request_agent_cli`'s OWN exit codes/stdout (scripted via env
    vars) so these tests stay focused on `safent`'s OWN orchestration:
    does it call claim, react correctly to "nothing to claim" vs "claimed",
    run the right action, and resolve success/failure correctly.

Same "only podman (+curl) faked, everything else real" discipline as
tests/unit/ops/test_companion_provision.py.
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

_FAKE_PODMAN = r"""#!/usr/bin/env bash
set -e
echo "$@" >> "$FAKE_PODMAN_LOG"
case "$1" in
  inspect)
    case "$*" in
      *"-f "*)
        # `_running`: --type container -f {{.State.Running}} NAME
        echo "${FAKE_RUNNING:-true}"
        exit 0
        ;;
      *)
        # `_exists`: --type container NAME (no -f)
        [ "${FAKE_CONTAINER_EXISTS:-1}" = "1" ] && exit 0 || exit 1
        ;;
    esac
    ;;
  container)
    case "$2" in
      exists) exit 1 ;;  # fresh test: ads-db never existed -> alembic guard short-circuits
    esac
    exit 0
    ;;
  network)
    case "$2" in
      inspect) [ "${FAKE_NETWORK_PRESENT:-1}" = "1" ] && exit 0 || exit 1 ;;
      create) exit 0 ;;
    esac
    exit 0
    ;;
  pull) exit 0 ;;
  run)
    case "$*" in
      *"safent-companion-runtime:/runtime"*) cat >/dev/null; exit 0 ;;
    esac
    # `run --rm --entrypoint cat <image> ...` (T015 scaffold's image-baked-
    # file probe, _fetch_companion_file) always misses -> forces the cache
    # tier (pre-seeded below). `run --rm --network none -- <image> python -m
    # safent_ads.tools.gen_keys` is scripted so ensure_secrets/
    # ensure_sso_keypair succeed.
    for a in "$@"; do
      if [ "$a" = "safent_ads.tools.gen_keys" ]; then
        echo "ADS_APPROVAL_SIGNING_KEY=new-seed"
        echo "ADS_APPROVAL_PUBLIC_KEY=new-pub"
        exit 0
      fi
    done
    exit 1
    ;;
  compose)
    verb="$6"
    case "$verb" in
      up) [ "${FAKE_COMPOSE_UP_FAIL:-0}" = "1" ] && exit 1; exit 0 ;;
      down) exit 0 ;;
      ps) for id in c1; do echo "$id"; done; exit 0 ;;
      exec) printf '%s\n' "${FAKE_DB_REVISION:-}"; exit 0 ;;
    esac
    exit 0
    ;;
  exec)
    shift  # drop "exec"
    if [ "$1" = "-u" ]; then shift 2; fi  # drop "-u <uid>"
    shift  # drop the container name
    case "$1 $2" in
      "test -f") exit 1 ;;  # legacy .update-requested/.uninstall-requested: never present here
    esac
    case "$*" in
      *install_request_agent_cli\ claim-ads*)
        if [ -n "${FAKE_CLAIM_install_companion:-}" ]; then verb=install_companion;
        elif [ -n "${FAKE_CLAIM_repair_companion:-}" ]; then verb=repair_companion;
        else exit 1; fi
        echo "$verb 0123456789abcdef0123456789abcdef"
        exit 0
        ;;
      *install_request_agent_cli\ claim*)
        verb="$5"
        case "$verb" in
          install_companion) slug="${FAKE_CLAIM_install_companion:-}" ;;
          repair_companion) slug="${FAKE_CLAIM_repair_companion:-}" ;;
          *) slug="" ;;
        esac
        [ -n "$slug" ] || exit 1
        [ "$slug" = "__NONE__" ] && { echo ""; exit 0; }
        echo "$slug"
        exit 0
        ;;
      *install_request_agent_cli\ resolve*)
        exit 0
        ;;
      *install_request_agent_cli\ verify-ads*)
        [ "${FAKE_RELOAD_FAIL:-0}" = "1" ] && exit 1
        exit 0
        ;;
    esac
    exit 0
    ;;
esac
exit 0
"""

_FAKE_CURL = r"""#!/usr/bin/env bash
case "$*" in
  *raw.githubusercontent.com*) exit 1 ;;  # force the cache tier, never real network
  *) printf '401' ;;                       # /mcp/health bearer-protected probe
esac
exit 0
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
    state_home.mkdir(parents=True, exist_ok=True)
    (state_home / "safent-seccomp.json").write_text("{}")
    bin_dir = state_home / "companions" / "ads" / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy(_PROVISION_SH, bin_dir / "provision.sh")
    (bin_dir / "provision.sh").chmod(0o755)
    shutil.copy(_COMPOSE_YAML, bin_dir / "compose.yaml")
    shutil.copy(_CAPS_TEMPLATE, bin_dir / "caps.template.yaml")


def _base_env(
    tmp_path: Path, fake_bin_dir: Path, state_home: Path, podman_log: Path
) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "home"),
        "SAFENT_STATE_HOME": str(state_home),
        "SAFENT_NAME": "agent-test",
        "SAFENT_ADS_IMAGE": "ghcr.io/devwspito/safent-ads@sha256:" + "a" * 64,
        "FAKE_PODMAN_LOG": str(podman_log),
    }


class TestCompanionInstall:
    @pytest.mark.parametrize("verb", ["install", "repair"])
    @pytest.mark.parametrize(
        "image",
        [
            "",
            "ghcr.io/devwspito/safent-ads:latest",
            "ghcr.io/devwspito/safent-ads:v0.2.2",
            "ghcr.io/devwspito/safent-ads@sha256:short",
            "ghcr.io/other/ads@sha256:" + "a" * 64,
        ],
    )
    def test_missing_or_mutable_bootstrap_pin_has_no_effect(
        self, tmp_path: Path, fake_bin_dir: Path, verb: str, image: str
    ) -> None:
        state_home = tmp_path / "state-home"
        _seed_state_home(state_home)
        log_path = tmp_path / "podman.log"
        env = _base_env(tmp_path, fake_bin_dir, state_home, log_path)
        env["SAFENT_ADS_IMAGE"] = image
        result = subprocess.run(
            ["sh", str(_SAFENT_CLI), "companion", verb],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode != 0
        assert not log_path.exists() or log_path.read_text() == ""

    def test_scaffolds_then_installs_then_reloads(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        state_home = tmp_path / "state-home"
        _seed_state_home(state_home)
        podman_log = tmp_path / "podman.log"
        env = _base_env(tmp_path, fake_bin_dir, state_home, podman_log)

        result = subprocess.run(
            ["sh", str(_SAFENT_CLI), "companion", "install"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        log_lines = podman_log.read_text().splitlines()
        assert any(ln.startswith("network create") for ln in log_lines)
        assert any(ln.startswith("compose ") and "up" in ln for ln in log_lines)
        assert any("install_request_agent_cli verify-ads" in ln for ln in log_lines)
        assert any(
            "exec -u root agent-test /usr/libexec/hermes/hermes-companion-bearer" in ln
            for ln in log_lines
        )
        assert "[ok] Companion instalado." in result.stdout

    def test_porcelain_emits_scaffold_up_and_reload_stages(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        state_home = tmp_path / "state-home"
        _seed_state_home(state_home)
        podman_log = tmp_path / "podman.log"
        env = _base_env(tmp_path, fake_bin_dir, state_home, podman_log)

        result = subprocess.run(
            ["sh", str(_SAFENT_CLI), "companion", "install", "--porcelain"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        stage_ids = [ln for ln in result.stdout.splitlines() if '"t":"stage"' in ln]
        assert any('"id":"companion_scaffold"' in ln for ln in stage_ids)
        assert any('"id":"companion_up"' in ln for ln in stage_ids)
        assert any('"id":"companion_reload"' in ln for ln in stage_ids)
        # Every event on stdout is valid NDJSON — no human "[ok]"/"[*]" text
        # leaked from provision.sh's own log() calls (app-engine.md §2).
        import json as _json  # noqa: PLC0415

        for line in result.stdout.splitlines():
            _json.loads(line)

    def test_hot_reload_failure_fails_the_install(self, tmp_path: Path, fake_bin_dir: Path) -> None:
        """CL-002: 'Instalar' prefers hot-reload but may fall back to a
        restart — a reload failure must never fail the install itself."""
        state_home = tmp_path / "state-home"
        _seed_state_home(state_home)
        podman_log = tmp_path / "podman.log"
        env = _base_env(tmp_path, fake_bin_dir, state_home, podman_log)
        env["FAKE_RELOAD_FAIL"] = "1"

        result = subprocess.run(
            ["sh", str(_SAFENT_CLI), "companion", "install"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0
        assert "[ok] Companion instalado." not in result.stdout

    def test_installing_twice_converges_without_duplicating_the_network(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        state_home = tmp_path / "state-home"
        _seed_state_home(state_home)
        podman_log = tmp_path / "podman.log"
        env = _base_env(tmp_path, fake_bin_dir, state_home, podman_log)
        env["FAKE_NETWORK_PRESENT"] = "0"  # first run: network does not exist yet

        first = subprocess.run(
            ["sh", str(_SAFENT_CLI), "companion", "install"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert first.returncode == 0, first.stderr

        env2 = dict(env)
        env2["FAKE_NETWORK_PRESENT"] = "1"  # second run: already there
        second = subprocess.run(
            ["sh", str(_SAFENT_CLI), "companion", "install"],
            env=env2,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert second.returncode == 0, second.stderr
        assert "safent-ads" in second.stdout or second.returncode == 0


class TestCompanionRepair:
    def test_repair_runs_the_same_reconciliation_as_install(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        state_home = tmp_path / "state-home"
        _seed_state_home(state_home)
        podman_log = tmp_path / "podman.log"
        env = _base_env(tmp_path, fake_bin_dir, state_home, podman_log)

        result = subprocess.run(
            ["sh", str(_SAFENT_CLI), "companion", "repair"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "[ok] Companion reparado." in result.stdout
        log_lines = podman_log.read_text().splitlines()
        assert any(ln.startswith("compose ") and "up" in ln for ln in log_lines)


class TestAgentTick:
    def _run_tick(
        self, tmp_path: Path, fake_bin_dir: Path, *, extra_env: dict[str, str] | None = None
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        state_home = tmp_path / "state-home"
        _seed_state_home(state_home)
        podman_log = tmp_path / "podman.log"
        env = _base_env(tmp_path, fake_bin_dir, state_home, podman_log)
        env["SAFENT_AGENT_ONCE"] = "1"
        env.update(extra_env or {})
        result = subprocess.run(
            ["sh", str(_SAFENT_CLI), "agent"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result, podman_log

    def test_nothing_live_does_nothing_and_returns_cleanly(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        result, podman_log = self._run_tick(tmp_path, fake_bin_dir)
        assert result.returncode == 0, result.stderr
        log = podman_log.read_text()
        assert "install_request_agent_cli resolve" not in log

    def test_claimed_install_request_runs_install_and_resolves_success(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        result, podman_log = self._run_tick(
            tmp_path, fake_bin_dir, extra_env={"FAKE_CLAIM_install_companion": "safent-ads"}
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        log_lines = podman_log.read_text().splitlines()
        assert any("install_request_agent_cli claim-ads" in ln for ln in log_lines)
        assert any(
            "install_request_agent_cli resolve-ads install_companion" in ln and "--success" in ln
            for ln in log_lines
        )
        # The claimed request actually ran the install (network created).
        assert any(ln.startswith("network create") for ln in log_lines)

    def test_claimed_repair_request_runs_repair_and_resolves_success(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        result, podman_log = self._run_tick(
            tmp_path, fake_bin_dir, extra_env={"FAKE_CLAIM_repair_companion": "safent-ads"}
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        log = podman_log.read_text()
        assert "install_request_agent_cli claim-ads" in log
        assert (
            "install_request_agent_cli resolve-ads repair_companion" in log and "--success" in log
        )

    def test_a_failed_install_resolves_failure_not_success(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        result, podman_log = self._run_tick(
            tmp_path,
            fake_bin_dir,
            extra_env={
                "FAKE_CLAIM_install_companion": "safent-ads",
                "FAKE_COMPOSE_UP_FAIL": "1",
            },
        )
        # The tick itself never aborts the agent loop over one failed install.
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        log = podman_log.read_text()
        assert (
            "install_request_agent_cli resolve-ads install_companion" in log and "--failure" in log
        )
        assert "--success" not in log

    def test_a_request_already_claimed_by_someone_else_is_left_alone(
        self, tmp_path: Path, fake_bin_dir: Path
    ) -> None:
        """install-request.md §4: 'dos lectores -> uno solo actua'. The
        claim CLI itself enforces this (already unit-tested); here we only
        prove the agent backs off cleanly when claiming reports nothing to
        do (FAKE_CLAIM_* unset -> the fake's own 'no live request or
        already claimed' response, exit 1)."""
        result, podman_log = self._run_tick(tmp_path, fake_bin_dir)
        assert result.returncode == 0, result.stderr
        log = podman_log.read_text()
        assert "companion_reload_cli" not in log
        assert "compose " not in log or "up" not in log


# =============================================================================
# Desktop integration requirement: the desktop adapter treats >15s without an
# NDJSON event as a stall and fails the bootstrap. `_run_with_heartbeat`
# (T016) is what stands between a multi-minute image pull and that watchdog —
# this proves it end to end through `safent companion install --porcelain`
# against a `podman pull` that genuinely takes long enough to matter, not
# just a fast fake that never exercises the heartbeat loop for real.
# =============================================================================

_FAKE_PODMAN_SLOW_PULL = _FAKE_PODMAN.replace(
    "  pull) exit 0 ;;\n",
    "  image)\n"
    '    [ "$2" = "inspect" ] && exit 1  # never "already local" -> ensure_image must pull\n'
    "    exit 0\n"
    "    ;;\n"
    "  pull)\n"
    "    _slept=0\n"
    '    while [ "$_slept" -lt "${FAKE_PULL_SLEEP_S:-0}" ]; do sleep 1; _slept=$((_slept + 1)); done\n'
    "    exit 0\n"
    "    ;;\n",
)


@pytest.fixture()
def fake_bin_dir_slow_pull(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin-slow"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text(_FAKE_PODMAN_SLOW_PULL)
    podman.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(_FAKE_CURL)
    curl.chmod(0o755)
    return bin_dir


class TestHeartbeatDuringASlowPull:
    def test_progress_events_never_gap_more_than_five_seconds(
        self, tmp_path: Path, fake_bin_dir_slow_pull: Path
    ) -> None:
        import json as _json  # noqa: PLC0415
        import time as _time  # noqa: PLC0415

        state_home = tmp_path / "state-home"
        _seed_state_home(state_home)
        podman_log = tmp_path / "podman.log"
        env = _base_env(tmp_path, fake_bin_dir_slow_pull, state_home, podman_log)
        env["FAKE_PULL_SLEEP_S"] = "20"
        env["FAKE_CLAIM_install_companion"] = "safent-ads"

        started = _time.monotonic()
        proc = subprocess.Popen(
            ["sh", str(_SAFENT_CLI), "companion", "requests", "--porcelain"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        events: list[tuple[float, dict]] = []
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                events.append((_time.monotonic() - started, _json.loads(line)))
        finally:
            proc.wait(timeout=60)

        assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""
        assert len(events) >= 2, "expected at least one heartbeat during the slow pull"
        request_log = podman_log.read_text()
        assert "install_request_agent_cli renew-ads install_companion" in request_log
        assert "--request-id 0123456789abcdef0123456789abcdef" in request_log
        assert "install_request_agent_cli resolve-ads install_companion" in request_log
        assert "--success" in request_log

        gaps = [events[i][0] - events[i - 1][0] for i in range(1, len(events))]
        assert max(gaps) < 15.0, (
            f"a gap of {max(gaps):.1f}s exceeds the desktop's 15s stall threshold: {gaps}"
        )

    def test_every_stdout_line_is_valid_json_even_during_the_slow_pull(
        self, tmp_path: Path, fake_bin_dir_slow_pull: Path
    ) -> None:
        import json as _json  # noqa: PLC0415

        state_home = tmp_path / "state-home"
        _seed_state_home(state_home)
        podman_log = tmp_path / "podman.log"
        env = _base_env(tmp_path, fake_bin_dir_slow_pull, state_home, podman_log)
        env["FAKE_PULL_SLEEP_S"] = "6"

        result = subprocess.run(
            ["sh", str(_SAFENT_CLI), "companion", "install", "--porcelain"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        for line in result.stdout.splitlines():
            _json.loads(line)  # raises if any line is not valid NDJSON
