"""Explicit source/wheel identity for a disposable guest; no implicit HEAD claim."""

import hashlib
import json
import re
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_inputs(root: Path, revision: str, overlays: list[str]) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Use the full revision returned by git rev-parse HEAD at archive creation")
    source = root / "source"
    files = {
        str(path.relative_to(source)): sha256(path)
        for path in sorted((source / "src").rglob("*.py"))
    }
    if not files:
        raise ValueError("Missing actual source snapshot")
    overlay_hashes = {}
    for name in overlays:
        path = source / name
        if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink():
            raise ValueError("Overlay must name an explicit source snapshot file")
        overlay_hashes[name] = sha256(path)
    wheel = root / "wheels/hermes_runtime-0.9.0-py3-none-any.whl"
    result = {
        "base_revision": revision,
        "source_sha256": files,
        "overlay_sha256": overlay_hashes,
        "wheel_sha256": sha256(wheel),
        "harness_sha256": {
            path.name: sha256(path)
            for path in sorted((root / "harness").glob("*"))
            if path.is_file() and path.suffix in {".py", ".sh"}
        },
    }
    with (root / "input-manifest.json").open("x") as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
    return result


def read_inputs(root: Path) -> dict:
    result = json.loads((root / "input-manifest.json").read_text())
    if not re.fullmatch(r"[0-9a-f]{40}", result["base_revision"]):
        raise ValueError("Invalid archived source revision")
    for name, digest in {**result["source_sha256"], **result["overlay_sha256"]}.items():
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Invalid source manifest path")
        if sha256(root / "source" / name) != digest:
            raise ValueError("Source changed since fixture inputs were recorded")
    if sha256(root / "wheels/hermes_runtime-0.9.0-py3-none-any.whl") != result["wheel_sha256"]:
        raise ValueError("Wheel changed since fixture inputs were recorded")
    for name, digest in result["harness_sha256"].items():
        if Path(name).name != name or sha256(root / "harness" / name) != digest:
            raise ValueError("Harness changed since fixture inputs were recorded")
    return result
