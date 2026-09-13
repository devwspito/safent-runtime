"""Opt-in real private-engine QA; never mounts the production projection.

SAFENT_QA_PROJECTION_PODMAN=/absolute/podman SAFENT_QA_IMAGE=repo@sha256:...
python3 -m unittest this_file.py (or pytest). Images must already be local.
"""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid


@unittest.skipUnless(os.environ.get("SAFENT_QA_PROJECTION_PODMAN"), "explicit isolated-engine QA only")
class RealProjectionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = os.environ["SAFENT_QA_PROJECTION_PODMAN"]
        self.assertTrue(Path(self.runtime).is_absolute())
        self.image = os.environ["SAFENT_QA_IMAGE"]
        self.assertRegex(self.image, r"^ghcr\.io/devwspito/safent(?:-ads)?@sha256:[0-9a-f]{64}$")
        self.call("image", "inspect", self.image)
        source = Path(os.environ.get("SAFENT_QA_PROVISION_SCRIPT", str(
            Path(__file__).resolve().parents[3] / "ops/container/companions/ads/provision.sh"
        ))).read_text()
        self.functions = "ensure_runtime_projection_volume() {" + source.split(
            "ensure_runtime_projection_volume() {", 1
        )[1].split("# Idempotent single-line writer:", 1)[0]
        self.volume = "safent-qa-projection-" + uuid.uuid4().hex
        self.assertNotEqual(self.volume, "safent-companion-runtime")
        self.assertNotEqual(self.call("volume", "inspect", self.volume, check=False).returncode, 0)
        self.created = False
        self.expected_label = "ads-runtime-projection"
        self.temp = tempfile.TemporaryDirectory(prefix="safent-projection-qa-")
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        (self.state / "tls").mkdir()
        (self.state / "sso").mkdir()
        for file, content in [("companions.json", "{}"), ("tls/ca.crt", "fake-ca"),
                              ("bearer", "fake-bearer"), ("sso/ads-sso.key", "fake-key")]:
            (self.state / file).write_text(content)
        self.addCleanup(self.clean_volume)

    def call(self, *args, check=True):
        result = subprocess.run([self.runtime, *args], capture_output=True, text=True, timeout=60)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def clean_volume(self):
        if not self.created:
            return
        identity = json.loads(self.call("volume", "inspect", self.volume).stdout)[0]
        self.assertEqual(identity["Name"], self.volume)
        self.assertEqual((identity.get("Labels") or {}).get("com.safent.component"), self.expected_label)
        self.call("volume", "rm", self.volume)  # no force; fail if a container remains attached

    def publish(self):
        return subprocess.run(["bash", "-c", "set -euo pipefail\n"
            + 'fail() { printf "%s\\n" "$*" >&2; exit 1; }\n'
            + self.functions + "\npublish_runtime_projection\n"],
            env={**os.environ, "RUNTIME": self.runtime, "SAFENT_IMAGE": self.image,
                 "SAFENT_ADS_IMAGE": self.image, "COMPANION_RUNTIME_VOLUME": self.volume,
                 "STATE": str(self.state)}, capture_output=True, text=True, timeout=60)

    def inside(self, command):
        return self.call("run", "--rm", "--network", "none", "--user", "0:0", "--read-only",
                         "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                         "-v", self.volume + ":/runtime", "--entrypoint", "/bin/sh", self.image,
                         "-ec", command)

    def test_create_publish_twice_and_recover_readonly_partial_stage(self):
        self.created = True  # inspect cleanup refuses if creation itself did not succeed
        for _ in range(2):
            result = self.publish()
            self.assertEqual(result.returncode, 0, result.stderr)
        first = self.inside("sha256sum /runtime/ads.bearer /runtime/ads-sso.key").stdout
        self.inside("umask 077; mkdir /runtime/.next; printf stale > /runtime/.next/ads.bearer; chmod 0400 /runtime/.next/ads.bearer")
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.inside("sha256sum /runtime/ads.bearer /runtime/ads-sso.key").stdout, first)
        self.inside('test "$(stat -c %a /runtime/ads.bearer)" = 400; test "$(cat /runtime/.next/ads.bearer)" = stale')
        self.assertFalse(list(self.state.glob(".runtime.*")))

    def test_foreign_and_missing_labels_are_not_adopted(self):
        # One fresh UUID per test iteration, and cleanup always validates exact identity.
        for label in ["foreign-qa", "", None]:
            self.expected_label = label
            args = ["volume", "create"]
            if label is not None:
                args += ["--label", "com.safent.component=" + label]
            self.call(*args, self.volume)
            self.created = True
            result = self.publish()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no pertenece", result.stderr)
            self.clean_volume()
            self.created = False


if __name__ == "__main__":
    unittest.main(verbosity=2)
