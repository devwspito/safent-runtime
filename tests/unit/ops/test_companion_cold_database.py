"""Cold startup starts only PostgreSQL before checking migration compatibility."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PROVISION = ROOT / "ops/container/companions/ads/provision.sh"
pytestmark = pytest.mark.unit


def exercise(tmp_path: Path, mode: str = "compatible"):
    source = PROVISION.read_text()
    names = ["ensure_database_running", "_refuse_if_image_predates_the_database", "start_companion"]
    functions = []
    for name in names:
        # Also execute against the pre-fix source to prove the regression.
        if name + "() {" in source:
            functions.append(name + "() {" + source.split(name + "() {", 1)[1].split("\n}", 1)[0] + "\n}")
    runtime = tmp_path / "runtime"
    runtime.write_text(r'''#!/usr/bin/env python3
import os, pathlib, sys
a=sys.argv[1:]; state=pathlib.Path(os.environ['QA_STATE']); mode=os.environ['QA_MODE']
with (state/'calls').open('a') as f: f.write(' '.join(a)+'\n')
if a[:2]==['container','exists']: sys.exit(0)
if a[0]=='compose':
 if a[5:]==['up','-d','--no-recreate','ads-db']:
  if mode=='start-failed': sys.exit(1)
  (state/'db-started').touch(); sys.exit(0)
 if a[5:]==['up','-d']:
  (state/'all-started').touch(); sys.exit(0)
 if 'pg_isready' in a:
  sys.exit(1 if mode=='not-ready' else 0)
if a[0]=='exec':
 if not (state/'db-started').exists(): sys.exit(125)
 if 'to_regclass' in a[-1]:
  if mode=='catalog-failed': sys.exit(1)
  print('' if mode=='fresh' else 'alembic_version'); sys.exit(0)
 if 'version_num' in a[-1]:
  if mode=='revision-failed': sys.exit(1)
  print('' if mode=='empty-table' else '0033_known'); sys.exit(0)
if a[0]=='run' and a[-2:]==['alembic','history']:
 if mode=='history-failed': sys.exit(1)
 if mode!='empty-history': print('0001 -> 0002_old' if mode=='older-image' else '0032 -> 0033_known, migration')
 sys.exit(0)
sys.exit(97)
''')
    runtime.chmod(0o755)
    (tmp_path / "pg_password").write_text("synthetic-test-password")
    script = r'''
set -euo pipefail
fail() { printf '%s\n' "$*" >&2; exit 1; }
log() { :; }
sleep() { :; }
reconcile_reserved_addresses() {
  printf '%s\n' preflight >> "$QA_STATE/calls"
  [ "$QA_MODE" != foreign-reservation ]
}
'''
    result = subprocess.run(
        ["bash", "-c", script + "\n".join(functions) + "\nstart_companion"],
        env={**os.environ, "RUNTIME": str(runtime), "QA_STATE": str(tmp_path),
             "STATE": str(tmp_path), "HERE": str(PROVISION.parent), "QA_MODE": mode,
             "SAFENT_ADS_IMAGE": "ghcr.io/devwspito/safent-ads@sha256:" + "a" * 64},
        capture_output=True, text=True, timeout=10, check=False,
    )
    return result, (tmp_path / "calls").read_text().splitlines()


@pytest.mark.parametrize("mode", ["compatible", "fresh", "empty-table"])
def test_database_resume_precedes_guard_and_all_services(tmp_path, mode):
    result, calls = exercise(tmp_path, mode)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[0] == "preflight"
    db = next(i for i, line in enumerate(calls) if "up -d --no-recreate ads-db" in line)
    ready = next(i for i, line in enumerate(calls) if "pg_isready" in line)
    guard = next(i for i, line in enumerate(calls) if "to_regclass" in line)
    assert db < ready < guard < len(calls) - 1
    assert calls[-1].endswith("up -d")
    assert not any(line.startswith(("rm ", "stop ", "volume rm ", "network rm ")) for line in calls)
    assert "synthetic-test-password" not in result.stdout + result.stderr


@pytest.mark.parametrize("mode", [
    "foreign-reservation", "start-failed", "not-ready", "catalog-failed",
    "revision-failed", "history-failed", "empty-history", "older-image",
])
def test_uncertainty_never_starts_migrations_or_advertising_services(tmp_path, mode):
    result, calls = exercise(tmp_path, mode)
    assert result.returncode != 0
    assert not any(line.endswith("up -d") for line in calls)
    assert not any(line.startswith(("rm ", "stop ", "volume rm ", "network rm ")) for line in calls)
    if mode == "foreign-reservation":
        assert result.returncode == 78
        assert calls == ["preflight"]
    if mode == "not-ready":
        assert sum("pg_isready" in line for line in calls) == 45
        assert not any("to_regclass" in line for line in calls)
