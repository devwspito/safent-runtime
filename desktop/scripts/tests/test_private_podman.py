import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("verify", Path(__file__).parents[1] / "lib/verify-private-podman.py")
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class PrivatePodmanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in verify.FILES:
            (self.root / name).write_text("fixture")
        (self.root / "podman").write_bytes(struct.pack("<II", 0xFEEDFACF, 0x0100000C))
        self.meta = {"schema_version": 1, "capability": "safent-private-machine-v1",
                     "source_commit": "source", "source_sha256": "sourcehash",
                     "patch_sha256": verify.digest(self.root / "podman-private.patch"),
                     "go_version": "1.25.9", "go_sha256": "toolchainhash",
                     "binary_sha256": verify.digest(self.root / "podman"),
                     "target": "aarch64-apple-darwin", "signed": False}
        self.lock = self.root / "lock.json"
        self.lock.write_text(json.dumps({"targets": {"aarch64-apple-darwin": {"private_build": self.meta}}}))
        self.save()

    def save(self):
        (self.root / "podman-private-build.json").write_text(json.dumps(self.meta))

    def test_verified_fixture(self):
        self.assertEqual(verify.verify(self.root, self.lock), self.meta)

    def test_self_asserted_binary_hash_cannot_override_lock(self):
        (self.root / "podman").write_bytes(b"stock or malicious executable")
        self.meta["binary_sha256"] = verify.digest(self.root / "podman")
        self.save()
        with self.assertRaisesRegex(ValueError, "pin mismatch"):
            verify.verify(self.root, self.lock)

    def test_binary_tampering(self):
        (self.root / "podman").write_bytes(b"stock")
        with self.assertRaisesRegex(ValueError, "binary SHA"):
            verify.verify(self.root, self.lock)

    def test_every_pin_must_match(self):
        for key in ("source_commit", "source_sha256", "patch_sha256", "go_version", "go_sha256"):
            with self.subTest(key=key):
                old = self.meta[key]
                self.meta[key] = "tampered"
                self.save()
                with self.assertRaises(ValueError):
                    verify.verify(self.root, self.lock)
                self.meta[key] = old

    def test_no_linked_artifact(self):
        (self.root / "podman-LICENSE").unlink()
        (self.root / "podman-LICENSE").symlink_to(self.lock)
        with self.assertRaisesRegex(ValueError, "linked"):
            verify.verify(self.root, self.lock)

    def test_capability_and_target_required(self):
        for key, value in (("capability", "stock"), ("target", "linux"), ("schema_version", True), ("signed", True)):
            old = self.meta[key]
            self.meta[key] = value
            self.save()
            with self.assertRaises(ValueError):
                verify.verify(self.root, self.lock)
            self.meta[key] = old

    def test_patch_tampering(self):
        (self.root / "podman-private.patch").write_text("different patch")
        with self.assertRaisesRegex(ValueError, "patch SHA"):
            verify.verify(self.root, self.lock)


if __name__ == "__main__":
    unittest.main()
