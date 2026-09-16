"""Native cache must match this bundle, not any previous Podman version."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.unit.cli.test_safent_porcelain import (
    _base_env,
    _make_bundle,
    fake_bin_dir,  # noqa: F401 -- shared real CLI fixture
)


def setup_bundle(tmp_path: Path, fake_bin_dir: Path) -> tuple[Path, Path, dict[str, str]]:
    bundle = _make_bundle(tmp_path, entries=[("docker-compose", b"bundled-compose", "0644")])
    home = tmp_path / "home"
    env = _base_env(fake_bin_dir=fake_bin_dir, home_dir=home, podman_log=tmp_path / "podman.log")
    return bundle, home / ".safent/runtime/6.1.1", env


def run(bundle: Path, env: dict[str, str], verb: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", str(bundle / "safent"), verb], env=env, capture_output=True, text=True, timeout=30)


def cache_current(bundle: Path, env: dict[str, str]) -> bool:
    result = run(bundle, env, "facts")
    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout)
    assert facts["runtimeStaged"] == facts["runtimeHashOk"]
    return bool(facts["runtimeHashOk"])


def test_same_podman_new_manifest_invalidates_cache_and_repairs_compose_mode(tmp_path: Path, fake_bin_dir: Path) -> None:
    bundle, target, env = setup_bundle(tmp_path, fake_bin_dir)
    assert run(bundle, env, "stage-runtime").returncode == 0
    assert cache_current(bundle, env)
    manifest = bundle / "runtime-bundle.json"
    content = json.loads(manifest.read_text())
    content["entries"][0]["mode"] = "0755"
    manifest.write_text(json.dumps(content, indent=2))
    assert not cache_current(bundle, env)
    assert run(bundle, env, "stage-runtime").returncode == 0
    assert (target / "docker-compose").stat().st_mode & 0o777 == 0o755
    assert cache_current(bundle, env)


@pytest.mark.parametrize("corruption", ["mode", "bytes", "legacy_marker", "other_version"])
def test_corrupt_or_legacy_cache_is_not_verified(tmp_path: Path, fake_bin_dir: Path, corruption: str) -> None:
    bundle, target, env = setup_bundle(tmp_path, fake_bin_dir)
    assert run(bundle, env, "stage-runtime").returncode == 0
    if corruption == "mode":
        (target / "docker-compose").chmod(0o755)
    elif corruption == "bytes":
        (target / "docker-compose").write_bytes(b"corrupt-compose")
    elif corruption == "legacy_marker":
        (target / ".verified").write_text("")
    else:
        target.rename(target.with_name("6.0.0"))
    assert not cache_current(bundle, env)


def test_symlink_cache_never_writes_an_external_file(tmp_path: Path, fake_bin_dir: Path) -> None:
    bundle, target, env = setup_bundle(tmp_path, fake_bin_dir)
    assert run(bundle, env, "stage-runtime").returncode == 0
    external = tmp_path / "unrelated"
    external.write_text("keep")
    (target / "docker-compose").unlink()
    (target / "docker-compose").symlink_to(external)
    assert not cache_current(bundle, env)
    assert run(bundle, env, "stage-runtime").returncode != 0
    assert external.read_text() == "keep"
