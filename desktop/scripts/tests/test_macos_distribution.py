import importlib.util
import json
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest

DESKTOP = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("distribution", DESKTOP / "scripts/validate-macos-distribution.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MacosDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "Safent.app"
        contents = self.app / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Resources").mkdir()
        self.exe = contents / "MacOS/safent-desktop"
        self.exe.write_bytes(b"not executed: metadata fixture only")
        self.exe.chmod(0o755)
        (contents / "Resources/icon.icns").write_bytes(b"fixture icon")
        self.info = contents / "Info.plist"
        self.data = {
            "CFBundleIdentifier": "com.safent.desktop", "CFBundlePackageType": "APPL",
            "CFBundleName": "Safent", "CFBundleExecutable": "safent-desktop",
            "CFBundleShortVersionString": "0.9.2", "CFBundleVersion": "0.9.2",
            "CFBundleIconFile": "icon.icns", "LSRequiresCarbon": True,
        }
        self.write_info()
        (self.root / "Applications").symlink_to("/Applications")

    def write_info(self):
        self.info.write_bytes(plistlib.dumps(self.data))

    def test_gui_metadata_accepts_tauri_legacy_carbon_without_hidden_app_flags(self):
        MODULE.validate_metadata(self.app, "0.9.2")
        MODULE.validate_dmg_root(self.root, self.app)

    def test_hidden_background_or_wrong_identity_rejected(self):
        for key, value in (
            ("LSUIElement", True), ("LSBackgroundOnly", True),
            ("LSUIElement", "false"), ("CFBundleIdentifier", "other.app"),
            ("CFBundleVersion", "0.9.1"), ("CFBundlePackageType", "BNDL"),
        ):
            with self.subTest(key=key, value=value):
                original = self.data.copy()
                self.data[key] = value
                self.write_info()
                with self.assertRaises(ValueError):
                    MODULE.validate_metadata(self.app, "0.9.2")
                self.data = original

    def test_non_executable_and_outside_icon_rejected(self):
        self.exe.chmod(0o644)
        with self.assertRaises(ValueError):
            MODULE.validate_metadata(self.app, "0.9.2")
        self.exe.chmod(0o755)
        self.data["CFBundleIconFile"] = "../other.icns"
        self.write_info()
        with self.assertRaises(ValueError):
            MODULE.validate_metadata(self.app, "0.9.2")

    def test_executable_directory_cannot_redirect_outside_bundle(self):
        macos = self.app / "Contents/MacOS"
        outside = self.root / "outside"
        macos.rename(outside)
        macos.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            MODULE.validate_metadata(self.app, "0.9.2")

    def test_wrong_destination_or_extra_installer_is_not_drag_only(self):
        shortcut = self.root / "Applications"
        shortcut.unlink()
        shortcut.symlink_to("/tmp")
        with self.assertRaises(ValueError):
            MODULE.validate_dmg_root(self.root, self.app)
        shortcut.unlink()
        shortcut.symlink_to("/Applications")
        (self.root / "Install.command").write_text("must never be shipped")
        with self.assertRaises(ValueError):
            MODULE.validate_dmg_root(self.root, self.app)

    def test_committed_layout_matches_bitmap_and_real_finder_items(self):
        config = json.loads((DESKTOP / "src-tauri/tauri.conf.json").read_text())
        self.assertEqual(config["identifier"], "com.safent.desktop")
        self.assertEqual(config["productName"], "Safent")
        dmg = config["bundle"]["macOS"]["dmg"]
        self.assertEqual(dmg["windowSize"], {"width": 660, "height": 400})
        self.assertEqual(dmg["appPosition"], {"x": 180, "y": 200})
        self.assertEqual(dmg["applicationFolderPosition"], {"x": 480, "y": 200})
        png = (DESKTOP / "src-tauri" / dmg["background"]).read_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (660, 400))
        # No custom plist overrides hide the app or duplicate Tauri's generated identity.
        self.assertNotIn("infoPlist", config["bundle"]["macOS"])
        self.assertFalse((DESKTOP / "src-tauri/Info.plist").exists())


if __name__ == "__main__":
    unittest.main()
