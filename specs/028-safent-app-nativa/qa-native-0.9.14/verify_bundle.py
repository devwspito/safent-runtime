"""Verify a Safent app bundle's declared files and immutable pins, read-only."""
import hashlib
import json
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

app = Path(sys.argv[1]).resolve()
engine_digest, ads_digest = sys.argv[2:4]
with (app / "Contents/Info.plist").open("rb") as stream:
    info = plistlib.load(stream)
assert info["CFBundleShortVersionString"] == "0.9.14"
runtime = app / "Contents/Resources/runtime"
bundle = json.loads((runtime / "runtime-bundle.json").read_text())
subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
resigned_macho = []
for key, repo, digest in (("engine_image", "ghcr.io/devwspito/safent", engine_digest),
                          ("companion_image", "ghcr.io/devwspito/safent-ads", ads_digest)):
    assert bundle[key] == {"repo": repo, "digest": digest, "platform": "linux/arm64"}, key
for entry in bundle["entries"]:
    path = runtime / entry["path"]
    assert path.resolve().is_relative_to(runtime.resolve()), entry["path"]
    assert not path.is_symlink(), entry["path"]
    assert stat.S_IMODE(path.stat().st_mode) == int(entry["mode"], 8), entry["path"]
    with path.open("rb") as stream:
        hasher = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
        digest = hasher.hexdigest()
    if digest != entry["sha256"]:
        # The manifest records build-time hashes. Apple signing changes Mach-O
        # bytes afterwards; the sealed app is the actual macOS integrity gate.
        with path.open("rb") as stream:
            magic = stream.read(4)
        assert magic in (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
                         b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"), entry["path"]
        subprocess.run(["codesign", "--verify", "--strict", str(path)], check=True)
        resigned_macho.append(entry["path"])
assert "ensure_runtime_projection_volume" in (runtime / "provision.sh").read_text()
assert "reconcile_reserved_addresses" in (runtime / "provision.sh").read_text()
assert '--ip-range "$COMPANION_DYNAMIC_RANGE"' in (runtime / "provision.sh").read_text()
assert "ipv4_address: 10.201.0.12" in (runtime / "compose.yaml").read_text()
cli = (runtime / "safent").read_text()
assert 'CORE_COMPANION_IP="10.201.0.2"' in cli
assert "_prepare_core_recreation && _core_address_available" in cli
assert "_core_network_matches && _companion_images_match" in cli
assert "se conserva el motor" in cli.lower()
print(json.dumps({"version": info["CFBundleShortVersionString"], "files_verified": len(bundle["entries"]), "resigned_macho_verified_by_apple_seal": resigned_macho,
                  "engine": bundle["engine_image"], "ads": bundle["companion_image"]}))
