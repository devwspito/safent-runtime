"""Pinned Compose staging uses the production downloader, offline file fixtures."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parents[2]


def test_release_pins_binary_and_redistribution_metadata():
    lock = json.loads((DESKTOP / "runtime-manifest.lock").read_text())
    provider = lock["targets"]["aarch64-apple-darwin"]["compose_provider"]
    assert provider["version"] == "5.5.1"
    assert provider["license_spdx"] == "Apache-2.0"
    assert (
        provider["download"]["sha256"]
        == "998735c9b6fe68a4f05895e6ea73d71ad06f9fc7046383ad89e47346781b6af5"
    )
    for name in ("download", "license", "provenance", "sbom"):
        assert provider[name]["url"].startswith(
            (
                "https://github.com/docker/compose/",
                "https://raw.githubusercontent.com/docker/compose/",
            )
        )
        assert len(provider[name]["sha256"]) == hashlib.sha256().digest_size * 2
        assert provider[name]["size_bytes"] > 0


@pytest.mark.parametrize("corrupt", [False, True])
def test_real_downloader_and_stager_fail_closed(tmp_path, corrupt):
    provider = {}
    paths = {
        "download": "bin/docker-compose",
        "license": "docker-compose-LICENSE",
        "provenance": "docker-compose-provenance.json",
        "sbom": "docker-compose-sbom.json",
    }
    for component, relative in paths.items():
        data = f"fixture-{component}\n".encode()
        source = tmp_path / component
        source.write_bytes(data)
        provider[component] = {
            "url": source.as_uri(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "path": relative,
        }
    if corrupt:
        provider["download"]["sha256"] = "0" * 64
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"targets": {"qa": {"compose_provider": provider}}}))
    destination = tmp_path / "output"
    script = (
        'source "$1/lib/fetch-verified.sh"; '
        'source "$1/lib/stage-compose-provider.sh"; '
        'stage_compose_provider "$2" qa "$3" "$4"'
    )
    result = subprocess.run(  # noqa: S603 - fixed shell program, own fixtures
        [
            "/bin/bash",
            "-c",
            script,
            "qa",
            str(DESKTOP / "scripts"),
            str(lock),  # noqa: S603 - fixed shell program, own fixtures
            str(tmp_path / "cache"),
            str(destination),
        ],
        env={**os.environ, "FETCH_VERIFIED_RETRY": "0", "FETCH_VERIFIED_OUTER_TRIES": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if corrupt:
        assert result.returncode == 3, result.stderr  # noqa: PLR2004 - downloader EXIT_INTEGRITY
        assert not (destination / "bin/docker-compose").exists()
    else:
        assert result.returncode == 0, result.stderr
        for component, relative in paths.items():
            assert (destination / relative).read_bytes() == (tmp_path / component).read_bytes()
        assert (destination / "bin/docker-compose").stat().st_mode & 0o111
        assert not (destination / "docker-compose-LICENSE").stat().st_mode & 0o111
