"""Install/repair must converge the core before any Ads Compose start.

Executes the production shell function; all runtime effects are closed stubs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


def exercise(
    tmp_path: Path, *, fail: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    source = (ROOT / "safent").read_text()
    name = "_companion_install_or_repair"
    function = name + "() {" + source.split(name + "() {", 1)[1].split("\n}", 1)[0] + "\n}"
    stubs = r"""
set -eu
_event() { printf '%s\n' "$1" >> "$EVENTS"; [ "$FAIL" != "$1" ]; }
_is_pinned_ads_image() { _event pinned; }
_fetch_companion_file() { :; }
_stage() { _active_stage=$1; _event "stage:$_active_stage"; }
_stage_done() { _event "done:$_active_stage"; }
_die_porcelain() { printf '%s\n' "$1" >&2; exit 79; }
_running() { return 0; }
_container_matches_desired() { _event topology; }
_companion_images_match() { _event images; }
fake_runtime() { _event staging; }
_run_with_heartbeat() {
  case "$*" in
    *--scaffold) _event scaffold ;;
    _companion_converge_core) _event converge ;;
    *provision.sh) _event compose ;;
    *verify-ads) _event verify ;;
    *) printf '%s\n' 'unexpected command in fixture' >&2; exit 98 ;;
  esac
}
"""
    events = tmp_path / "events"
    result = subprocess.run(
        ["sh", "-c", stubs + function + "\n_companion_install_or_repair"],
        env={
            **os.environ,
            "COMPANION_BIN_DIR": str(tmp_path / "bin"),
            "SAFENT_ADS_IMAGE": "ghcr.io/devwspito/safent-ads@sha256:" + "a" * 64,
            "RT": "fake_runtime",
            "NAME": "synthetic-core",
            "PORCELAIN": "1",
            "EVENTS": str(events),
            "FAIL": fail,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result, events.read_text().splitlines()


def test_core_convergence_precedes_compose_and_verified_registration(tmp_path):
    result, events = exercise(tmp_path)
    assert result.returncode == 0, result.stderr
    assert events == [
        "pinned",
        "stage:companion_scaffold",
        "scaffold",
        "converge",
        "done:companion_scaffold",
        "stage:companion_up",
        "compose",
        "done:companion_up",
        "stage:companion_reload",
        "topology",
        "images",
        "staging",
        "verify",
        "done:companion_reload",
    ]


def test_core_conflict_never_runs_compose(tmp_path):
    result, events = exercise(tmp_path, fail="converge")
    assert result.returncode == 79
    assert events == ["pinned", "stage:companion_scaffold", "scaffold", "converge"]
    assert "compose" not in events and "verify" not in events


def test_healthy_but_wrong_companion_image_does_not_register_success(tmp_path):
    result, events = exercise(tmp_path, fail="images")
    assert result.returncode == 79
    assert events[-1] == "images"
    assert "staging" not in events and "verify" not in events


def test_topology_change_during_compose_does_not_recreate_core_afterwards(tmp_path):
    result, events = exercise(tmp_path, fail="topology")
    assert result.returncode == 79
    assert events == [
        "pinned",
        "stage:companion_scaffold",
        "scaffold",
        "converge",
        "done:companion_scaffold",
        "stage:companion_up",
        "compose",
        "done:companion_up",
        "stage:companion_reload",
        "topology",
    ]
    assert events.count("converge") == 1
    assert "verify" not in events
