#!/usr/bin/env python3
"""Verify immutable build pins before staging, not a self-signed sidecar."""
import hashlib
import json
from pathlib import Path
import shutil
import struct
import sys


FILES = ("podman-private-build.json", "podman-LICENSE", "podman-private.patch",
         "podman-go-modules.jsonstream", "podman-vendor-modules.txt",
         "podman-sbom.cdx.json", "podman-third-party-notices.txt")


def digest(path):
    with path.open("rb") as stream:
        value = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
        return value.hexdigest()


def verify(artifact, lock):
    for name in ("podman", *FILES):
        path = artifact / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing/linked private build artifact: " + name)
    meta = json.loads((artifact / "podman-private-build.json").read_text())
    pin = json.loads(lock.read_text())["targets"]["aarch64-apple-darwin"]["private_build"]
    for key in ("source_commit", "source_sha256", "patch_sha256", "go_version", "go_sha256", "binary_sha256"):
        if meta.get(key) != pin[key]:
            raise ValueError("private Podman pin mismatch: " + key)
    if type(meta.get("schema_version")) is not int or meta["schema_version"] != 1 or meta.get("capability") != "safent-private-machine-v1":
        raise ValueError("private Podman capability missing")
    if meta.get("target") != "aarch64-apple-darwin" or meta.get("signed") is not False:
        raise ValueError("only pinned unsigned Darwin arm64 build is accepted")
    if digest(artifact / "podman") != pin["binary_sha256"]:
        raise ValueError("private Podman binary SHA mismatch")
    if digest(artifact / "podman-private.patch") != pin["patch_sha256"]:
        raise ValueError("private Podman patch SHA mismatch")
    with (artifact / "podman").open("rb") as stream:
        header = stream.read(8)
    if header != struct.pack("<II", 0xFEEDFACF, 0x0100000C):
        raise ValueError("private Podman is not a thin Darwin arm64 executable")
    return meta


def main():
    artifact, lock = map(Path, sys.argv[1:3])
    verify(artifact, lock)
    if len(sys.argv) == 4:
        destination = Path(sys.argv[3])
        shutil.copyfile(artifact / "podman", destination / "bin/podman")
        (destination / "bin/podman").chmod(0o755)
        for name in FILES:
            shutil.copyfile(artifact / name, destination / name)
    print("Verified private Podman source, patch, toolchain and binary pins")


if __name__ == "__main__":
    main()
