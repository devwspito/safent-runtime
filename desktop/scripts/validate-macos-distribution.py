#!/usr/bin/env python3
"""Read-only gate for the signed GUI app and standard drag-to-Applications DMG."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

IDENTIFIER = "com.safent.desktop"
TEAM = "JBMBA58A8X"


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
    args = parser.parse_args()
    validate_metadata(args.app, args.version)
    if args.dmg_root:
        validate_dmg_root(args.dmg_root, args.app)
    if sys.platform != "darwin":
        raise SystemExit("Signature verification requires macOS; metadata alone is not certification")
    requirement = f'=anchor apple generic and identifier "{IDENTIFIER}" and certificate leaf[subject.OU] = "{TEAM}"'
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", requirement, str(args.app)], check=True)
    print("GUI bundle metadata, layout (when supplied) and signature verified. No app was installed or opened.")


if __name__ == "__main__":
    main()
