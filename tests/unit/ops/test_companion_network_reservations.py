"""Legacy IPAM repair is bounded by exact container and compose ownership."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "ops/container/companions/ads/provision.sh"
COMPOSE = SCRIPT.with_name("compose.yaml")
pytestmark = pytest.mark.unit


def function(name: str) -> str:
    return name + "() {" + SCRIPT.read_text().split(name + "() {", 1)[1].split("\n}", 1)[0] + "\n}"


def exercise(tmp_path, records, *, changed=None, inventory_failure=False, inspect_failure=False):
    runtime = tmp_path / "runtime"
    runtime.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
a=sys.argv[1:]; p=pathlib.Path(os.environ['QA_STATE'])
d=json.loads(os.environ['QA_RECORDS']); log=p/'calls'
with log.open('a') as f: f.write(' '.join(a)+'\\n')
if a[0]=='ps':
 if os.environ.get('QA_PS_FAIL')=='1': sys.exit(1)
 print('\\n'.join(d)); sys.exit(0)
if a[0]=='inspect':
 if os.environ.get('QA_INSPECT_FAIL')=='1': sys.exit(1)
 cid=a[-1]; count=p/cid; n=int(count.read_text()) if count.exists() else 0
 count.write_text(str(n+1))
 changed=json.loads(os.environ['QA_CHANGED'])
 print(changed.get(cid,d[cid]) if n else d[cid]); sys.exit(0)
if a[0] in ('stop','rm'): sys.exit(0)
sys.exit(1)
""")
    runtime.chmod(0o755)
    env = {
        **os.environ,
        "QA_STATE": str(tmp_path),
        "QA_RECORDS": json.dumps(records),
        "QA_CHANGED": json.dumps(changed or {}),
        "QA_PS_FAIL": str(int(inventory_failure)),
        "QA_INSPECT_FAIL": str(int(inspect_failure)),
        "RUNTIME": str(runtime),
        "HERE": str(COMPOSE.parent),
        "COMPANION_NETWORK": "safent-companions",
    }
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            'fail() { echo "$*" >&2; exit 1; }; log() { :; };\n'
            + function("reconcile_reserved_addresses")
            + "\nreconcile_reserved_addresses",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    calls = (tmp_path / "calls").read_text().splitlines()
    return result, [c for c in calls if c.startswith(("stop ", "rm "))]


def row(cid, ip, role="ads-broker", project="safent-ads", config=None):
    return f"{cid}|{ip}|{project}|{role}|{config or COMPOSE}"


def test_product_reservations_and_dependencies():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    expected = {
        "ads-api": "10.201.0.10",
        "ads-db": "10.201.0.11",
        "ads-broker": "10.201.0.12",
        "ads-worker": "10.201.0.13",
        "ads-migrate": "10.201.0.14",
    }
    for service, ip in expected.items():
        assert services[service]["networks"]["safent-companions"]["ipv4_address"] == ip
    for service in ("ads-api", "ads-worker"):
        assert services[service]["depends_on"]["ads-broker"] == {"condition": "service_started"}


def test_new_network_excludes_reservations(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n[ "$2" != inspect ] || exit 1\n'
    )
    runtime.chmod(0o755)
    calls = tmp_path / "calls"
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            "log() { :; }; fail() { exit 1; };\n" + function("ensure_network") + "\nensure_network",
        ],
        env={
            **os.environ,
            "RUNTIME": str(runtime),
            "CALLS": str(calls),
            "COMPANION_SUBNET": "10.201.0.0/24",
            "COMPANION_GATEWAY": "10.201.0.1",
            "COMPANION_DYNAMIC_RANGE": "10.201.0.128/25",
            "COMPANION_NETWORK": "qa",
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--ip-range 10.201.0.128/25" in calls.read_text()


def test_crossed_broker_released_by_id_without_touching_core(tmp_path):
    broker, core = "a" * 64, "b" * 64
    result, mutations = exercise(
        tmp_path,
        {
            broker: row(broker, "10.201.0.10"),
            core: row(core, "10.201.0.9", "core", "other", "/other"),
        },
    )
    assert result.returncode == 0, result.stderr
    assert mutations == [f"stop --time 30 {broker}", f"rm {broker}"]


@pytest.mark.parametrize(
    "ip,role,project,config",
    [
        ("10.201.0.10", "core", "other", "/other"),
        ("10.201.0.12", "ads-db", "other", "/other"),
        ("10.201.0.10", "ads-broker", "safent-ads", "/another-install/compose.yaml"),
        ("10.201.0.10", "unknown", "safent-ads", None),
    ],
)
def test_foreign_occupant_blocks_before_any_mutation(tmp_path, ip, role, project, config):
    own, foreign = "a" * 64, "b" * 64
    result, mutations = exercise(
        tmp_path, {own: row(own, "10.201.0.10"), foreign: row(foreign, ip, role, project, config)}
    )
    assert result.returncode != 0
    assert mutations == []


@pytest.mark.parametrize("changed", ["different-id", "different-source", "fixed-now"])
def test_recheck_is_exact_and_never_removes_changed_snapshot(tmp_path, changed):
    cid = "a" * 64
    second = {
        "different-id": row("b" * 64, "10.201.0.10"),
        "different-source": row(cid, "10.201.0.10", config="/other"),
        "fixed-now": row(cid, "10.201.0.12"),
    }[changed]
    result, mutations = exercise(tmp_path, {cid: row(cid, "10.201.0.10")}, changed={cid: second})
    assert result.returncode != 0
    assert mutations == []


@pytest.mark.parametrize("failure", ["inventory_failure", "inspect_failure"])
def test_inventory_error_never_means_empty(tmp_path, failure):
    cid = "a" * 64
    result, mutations = exercise(tmp_path, {cid: row(cid, "10.201.0.10")}, **{failure: True})
    assert result.returncode != 0
    assert mutations == []


@pytest.mark.parametrize("record", ["a" * 64, "a" * 64 + "|||||", "a" * 64 + "||||\nextra"])
def test_malformed_inspection_denies(tmp_path, record):
    result, mutations = exercise(tmp_path, {"a" * 64: record})
    assert result.returncode != 0
    assert mutations == []


def test_correct_reservations_and_empty_inventory_are_noops(tmp_path):
    cid = "a" * 64
    result, mutations = exercise(tmp_path, {cid: row(cid, "10.201.0.12")})
    assert result.returncode == 0, result.stderr
    assert not mutations
    empty = tmp_path / "empty"
    empty.mkdir()
    result, mutations = exercise(empty, {})
    assert result.returncode == 0, result.stderr
    assert not mutations


def test_cli_protected_conflict_is_visible_and_not_retryable(tmp_path):
    source = (ROOT / "safent").read_text()
    body = (
        "_companion_install_or_repair() {"
        + source.split("_companion_install_or_repair() {", 1)[1].split("\n}", 1)[0]
        + "\n}"
    )
    shell = """set -eu
_is_pinned_ads_image() { return 0; }
_fetch_companion_file() { return 0; }
_stage() { :; }; _stage_done() { :; }
_run_with_heartbeat() { case "$*" in *--scaffold) return 0;; *) return 78;; esac; }
_die_porcelain() { printf '%s|%s|%s\\n' "$1" "$2" "$3"; exit 25; }
"""
    result = subprocess.run(
        ["sh", "-c", shell + body + "\n_companion_install_or_repair"],
        env={
            **os.environ,
            "SAFENT_ADS_IMAGE": "unused",
            "RT": "unused",
            "COMPANION_BIN_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 25
    assert result.stdout.startswith("companion_network_conflict|")
    assert result.stdout.rstrip().endswith("|false")
    assert "companion_migration_failed" not in result.stdout
