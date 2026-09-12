"""Release metadata agrees before any wheel, native bundle or OCI build."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


def version_metadata(root: Path) -> dict[str, str]:
    python = tomllib.loads((root / "pyproject.toml").read_text())
    package = json.loads((root / "desktop/package.json").read_text())
    npm_lock = json.loads((root / "desktop/package-lock.json").read_text())
    frontend = json.loads((root / "frontend/package.json").read_text())
    frontend_lock = json.loads((root / "frontend/package-lock.json").read_text())
    cargo = tomllib.loads((root / "desktop/src-tauri/Cargo.toml").read_text())
    cargo_lock = tomllib.loads((root / "desktop/src-tauri/Cargo.lock").read_text())
    tauri = json.loads((root / "desktop/src-tauri/tauri.conf.json").read_text())
    assignments = [
        node.value.value
        for node in ast.parse((root / "src/hermes/__init__.py").read_text()).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
    ]
    assert len(assignments) == 1, "Runtime must expose one literal build version"
    crates = [item for item in cargo_lock["package"] if item["name"] == cargo["package"]["name"]]
    assert len(crates) == 1, "Cargo lock must contain exactly one desktop package"
    return {
        "pyproject.toml": python["project"]["version"],
        "hermes.__version__": assignments[0],
        "desktop/package.json": package["version"],
        "desktop/package-lock.json": npm_lock["version"],
        "desktop/package-lock.json root": npm_lock["packages"][""]["version"],
        "frontend/package.json": frontend["version"],
        "frontend/package-lock.json": frontend_lock["version"],
        "frontend/package-lock.json root": frontend_lock["packages"][""]["version"],
        "desktop/src-tauri/Cargo.toml": cargo["package"]["version"],
        "desktop/src-tauri/Cargo.lock": crates[0]["version"],
        "desktop/src-tauri/tauri.conf.json": tauri["version"],
    }


def test_all_release_metadata_matches_authoritative_version():
    version = (ROOT / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version)
    for path, actual in version_metadata(ROOT).items():
        assert actual == version, f"{path}: {actual!r} differs from VERSION {version!r}"


def test_runtime_import_reports_the_release_version():
    from hermes import __version__

    assert __version__ == (ROOT / "VERSION").read_text().strip()
