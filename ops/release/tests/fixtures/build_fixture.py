#!/usr/bin/env python3
"""build_fixture.py <scenario> <output_dir> <tag> — build a fixture tree that
looks like the union of a safent-desktop.yml run's artifacts (per-platform
`Safent-*` directories, same names the real workflow uses) plus a
`release-manifests` directory for latest.json / runtime-manifest.json /
`.minisig` / SHA256SUMS — the workflow currently uploads those three straight
to the GitHub release rather than as a separate artifact (see README.md
"Assumptions"); this fixture exercises the contract either way, since
publish-desktop-release.sh locates files by name anywhere under the run's
download root.

The minisign signer below is a direct port of the one already established in
tests/unit/agents_os/test_system_update_manifest.py — same wire format as
hermes.shell_server.runtime_manifest.verify_minisign, no external `minisign`
binary needed, so fixtures are deterministic and hermetic.

Scenarios:
  valid          everything verifies; --dry-run and a real publish both succeed.
  url_mismatch   one platforms.*.url points at the wrong tag path.
  bad_signature  runtime-manifest.json.minisig is signed by a DIFFERENT key
                 than the one written to test-pubkey.pub.
  missing_dmg    no .dmg shipped at all (macOS is mandatory).

`size_overflow` is NOT a distinct fixture: the driver reuses `valid` and
overrides PUBLISH_RELEASE_SIZE_LIMIT_BYTES to something smaller than these
(deliberately tiny) dummy files, so no multi-gigabyte fixture is needed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ASSET_BASE_URL = "https://github.com/devwspito/safent-runtime/releases/download"
_KEY_ID = b"\x01\x02\x03\x04\x05\x06\x07\x08"
_OTHER_KEY_ID = b"\xff\xfe\xfd\xfc\xfb\xfa\xf9\xf8"


def _pubkey_text(public_key: Ed25519PublicKey, key_id: bytes = _KEY_ID) -> str:
    raw = b"Ed" + key_id + public_key.public_bytes_raw()
    return f"untrusted comment: test pubkey\n{base64.b64encode(raw).decode()}\n"


def _sign(file_bytes: bytes, private_key: Ed25519PrivateKey, *, key_id: bytes = _KEY_ID) -> str:
    digest = hashlib.blake2b(file_bytes, digest_size=64).digest()
    sig = private_key.sign(digest)
    sig_raw = b"ED" + key_id + sig
    trusted_comment = "test trusted comment"
    global_sig = private_key.sign(sig + trusted_comment.encode("utf-8"))
    return (
        "untrusted comment: test signature\n"
        f"{base64.b64encode(sig_raw).decode()}\n"
        f"trusted comment: {trusted_comment}\n"
        f"{base64.b64encode(global_sig).decode()}\n"
    )


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build(scenario: str, out_dir: Path, tag: str) -> None:
    version = tag.lstrip("v")
    mac_dir = out_dir / "Safent-macOS-arm64"
    lx64_dir = out_dir / "Safent-Linux-x64"
    larm_dir = out_dir / "Safent-Linux-arm64"
    manifests_dir = out_dir / "release-manifests"

    dmg_name = f"Safent_{version}_aarch64.dmg"
    mac_bundle_name = f"Safent_{version}_aarch64.app.tar.gz"
    amd64_deb = f"safent_{version}_amd64.deb"
    amd64_rpm = f"safent_{version}-1.x86_64.rpm"
    arm64_deb = f"safent_{version}_arm64.deb"
    arm64_rpm = f"safent_{version}-1.aarch64.rpm"

    assets: dict[str, bytes] = {}
    if scenario != "missing_dmg":
        assets[str(mac_dir / dmg_name)] = b"fake dmg installer bytes\n"
    assets[str(mac_dir / mac_bundle_name)] = b"fake macos updater bundle bytes\n"
    assets[str(lx64_dir / amd64_deb)] = b"fake amd64 deb bytes\n"
    assets[str(lx64_dir / amd64_rpm)] = b"fake amd64 rpm bytes\n"
    assets[str(larm_dir / arm64_deb)] = b"fake arm64 deb bytes\n"
    assets[str(larm_dir / arm64_rpm)] = b"fake arm64 rpm bytes\n"

    mac_sig = "test-signature-box-macos-aarch64"
    amd64_sig = "test-signature-box-linux-amd64"
    arm64_sig = "test-signature-box-linux-arm64"
    assets[str(mac_dir / f"{mac_bundle_name}.sig")] = (mac_sig + "\n").encode()
    assets[str(lx64_dir / f"{amd64_deb}.sig")] = (amd64_sig + "\n").encode()
    assets[str(larm_dir / f"{arm64_deb}.sig")] = (arm64_sig + "\n").encode()

    mac_url = f"{ASSET_BASE_URL}/{tag}/{mac_bundle_name}"
    amd64_url = f"{ASSET_BASE_URL}/{tag}/{amd64_deb}"
    arm64_url = f"{ASSET_BASE_URL}/{tag}/{arm64_deb}"
    if scenario == "url_mismatch":
        amd64_url = f"{ASSET_BASE_URL}/v0.0.0-wrong-tag/{amd64_deb}"

    latest_json = {
        "version": version,
        "notes": "fixture",
        "pub_date": "2026-09-11T00:00:00Z",
        "platforms": {
            "darwin-aarch64": {"signature": mac_sig, "url": mac_url},
            "linux-x86_64": {"signature": amd64_sig, "url": amd64_url},
            "linux-aarch64": {"signature": arm64_sig, "url": arm64_url},
        },
    }
    assets[str(manifests_dir / "latest.json")] = (
        json.dumps(latest_json, indent=2, sort_keys=True) + "\n"
    ).encode()

    runtime_manifest = {
        "schema_version": 1,
        "version": version,
        "engine": {"linux/arm64": "sha256:" + "1" * 64, "linux/amd64": "sha256:" + "2" * 64},
        "companion": {"safent-ads": {"linux/arm64": "sha256:" + "3" * 64, "linux/amd64": "sha256:" + "4" * 64}},
        "runtime_bundle": {"podman": "6.1.1", "machine_os": "6.1"},
        "min_app_version": version,
    }
    manifest_bytes = (json.dumps(runtime_manifest, indent=2, sort_keys=True) + "\n").encode()
    assets[str(manifests_dir / "runtime-manifest.json")] = manifest_bytes

    signing_key = Ed25519PrivateKey.generate()
    verifying_key = signing_key if scenario != "bad_signature" else Ed25519PrivateKey.generate()
    minisig_text = _sign(manifest_bytes, signing_key)
    assets[str(manifests_dir / "runtime-manifest.json.minisig")] = minisig_text.encode()

    for path_str, data in assets.items():
        _write(Path(path_str), data)

    pubkey_path = manifests_dir / "test-pubkey.pub"
    _write(pubkey_path, _pubkey_text(verifying_key.public_key()).encode())

    checksum_lines = []
    for path_str, data in sorted(assets.items()):
        digest = hashlib.sha256(data).hexdigest()
        checksum_lines.append(f"{digest}  {Path(path_str).name}\n")
    _write(manifests_dir / "SHA256SUMS", "".join(checksum_lines).encode())


def main() -> int:
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <scenario> <output_dir> <tag>", file=sys.stderr)
        return 2
    scenario, out_dir, tag = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    build(scenario, out_dir, tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
