#!/usr/bin/env python3
"""Read-only gate for the signed GUI app and standard drag-to-Applications DMG."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import stat
import sys
import tempfile

IDENTIFIER = "com.safent.desktop"
TEAM = "JBMBA58A8X"
COMPOSE_VERSION = "5.5.1"


def validate_compose(runtime: Path, *, staged: bool = False, execute: bool = False) -> None:
    """Permissions are a launch contract; a valid signature/hash alone is not.

    Never repairs an already signed tree. Only `version --short` is executed;
    it gets no user's HOME, Docker context, credentials or container socket.
    """
    binary = runtime / ("bin/docker-compose" if staged else "docker-compose")
    manifest = runtime / "runtime-bundle.json"
    paths = [runtime, manifest, binary]
    if staged:
        paths.append(runtime / "bin")
    if any(path.is_symlink() for path in paths) or not runtime.is_dir():
        raise ValueError("Compose resources must not redirect outside the bundle")
    if not binary.is_file() or stat.S_IMODE(binary.stat().st_mode) != 0o755:
        raise ValueError("Bundled docker-compose must be a regular executable at mode 0755")
    if not os.access(binary, os.X_OK):
        raise ValueError("Bundled docker-compose is not executable on this volume")
    if not manifest.is_file():
        raise ValueError("Missing runtime-bundle.json")
    bundle = json.loads(manifest.read_text())
    entries = bundle.get("entries") if isinstance(bundle, dict) else None
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise ValueError("Invalid runtime bundle entries")
    matches = [e for e in entries if e.get("path") == "docker-compose"]
    if len(matches) != 1 or matches[0].get("mode") != "0755":
        raise ValueError("Compose manifest must record exactly one mode 0755 entry")
    if execute:
        with tempfile.TemporaryDirectory(prefix="safent-compose-version-") as scratch:
            result = subprocess.run(
                [str(binary.resolve()), "version", "--short"],
                cwd=scratch,
                env={
                    "HOME": scratch, "PATH": "/usr/bin:/bin", "TMPDIR": scratch,
                    "DOCKER_CONFIG": scratch,
                    "DOCKER_HOST": "unix://" + scratch + "/no-engine.sock",
                },
                capture_output=True, text=True, timeout=15, check=True,
            )
            if result.stdout.strip() != COMPOSE_VERSION:
                raise ValueError("Bundled Compose version did not match the reviewed provider")


def validate_metadata(app: Path, version: str) -> None:
    if app.is_symlink() or not app.is_dir() or app.name != "Safent.app":
        raise ValueError("Expected a real Safent.app bundle")
    contents = app / "Contents"
    info = contents / "Info.plist"
    if any(path.is_symlink() for path in (contents, info, contents / "MacOS", contents / "Resources")):
        raise ValueError("Bundle metadata must not redirect outside the app")
    with info.open("rb") as stream:
        data = plistlib.load(stream)
    for key, expected in {
        "CFBundleIdentifier": IDENTIFIER,
        "CFBundlePackageType": "APPL",
        "CFBundleName": "Safent",
        "CFBundleExecutable": "safent-desktop",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
    }.items():
        if data.get(key) != expected:
            raise ValueError(f"Unexpected {key}")
    # Absent (Tauri default) or explicitly false are normal GUI applications.
    for key in ("LSUIElement", "LSBackgroundOnly"):
        if key in data and data[key] is not False:
            raise ValueError(f"GUI application must not enable {key}")
    executable = contents / "MacOS" / "safent-desktop"
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("Missing regular executable")
    icon = data.get("CFBundleIconFile")
    if not isinstance(icon, str) or Path(icon).name != icon or not icon.endswith(".icns"):
        raise ValueError("Expected a bundled icns icon")
    icon_path = contents / "Resources" / icon
    if icon_path.is_symlink() or not icon_path.is_file() or icon_path.stat().st_size == 0:
        raise ValueError("Missing bundled icon")
    validate_compose(contents / "Resources/runtime")


def validate_dmg_root(root: Path, app: Path) -> None:
    if app.parent.resolve() != root.resolve():
        raise ValueError("Safent.app must be at the volume root")
    shortcut = root / "Applications"
    if not shortcut.is_symlink() or os.readlink(shortcut) != "/Applications":
        raise ValueError("Applications shortcut must point directly to /Applications")
    visible = {item.name for item in root.iterdir() if not item.name.startswith(".")}
    if visible != {"Safent.app", "Applications"}:
        raise ValueError("DMG must only expose the app and Applications shortcut")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--dmg-root", type=Path)
    parser.add_argument("--staged-runtime", action="store_true",
                        help="Validate a staged runtime before codesigning (no app yet)")
    args = parser.parse_args()
    if args.staged_runtime:
        if args.dmg_root or sys.platform != "darwin":
            raise SystemExit("Staged native Compose verification requires macOS and no DMG root")
        validate_compose(args.app, staged=True, execute=True)
        print("Staged Compose executable and manifest verified before signing.")
        return
    validate_metadata(args.app, args.version)
    if args.dmg_root:
        validate_dmg_root(args.dmg_root, args.app)
    if sys.platform != "darwin":
        raise SystemExit("Signature verification requires macOS; metadata alone is not certification")
    requirement = f'=anchor apple generic and identifier "{IDENTIFIER}" and certificate leaf[subject.OU] = "{TEAM}"'
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", requirement, str(args.app)], check=True)
    validate_compose(args.app / "Contents/Resources/runtime", execute=True)
    print("GUI bundle metadata, layout (when supplied) and signature verified. No app was installed or opened.")


if __name__ == "__main__":
    main()
