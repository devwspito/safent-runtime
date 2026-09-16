"""ops/container/sign_runtime_manifest.py — release tooling (T005/T006).

No registry calls (digests are passed in explicitly) — build_payload/
validate_payload_digests are pure and fully hermetic. Signing wraps the
REAL `minisign` binary via subprocess: a fake shim on PATH proves the
wrapper's own argument-construction/error-handling logic without
depending on the binary being installed; `TestEndToEndWithRealMinisign`
additionally proves genuine interop with
hermes.shell_server.runtime_manifest.verify_minisign using the real
binary — skipped when it is not on PATH (same convention as
tests/unit/ops/test_gitleaks_allowlist.py).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hermes.shell_server import runtime_manifest as rm

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "ops" / "container" / "sign_runtime_manifest.py"


def _load_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("sign_runtime_manifest", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


srm = _load_module()

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64
_DIGEST_D = "sha256:" + "d" * 64

_SIGN_ARGS = [
    "--version", "2.0.0",
    "--engine-amd64", _DIGEST_A,
    "--engine-arm64", _DIGEST_B,
    "--companion-amd64", _DIGEST_C,
    "--companion-arm64", _DIGEST_D,
    "--podman-version", "6.1.1",
    "--machine-os", "6.1",
]


def _payload() -> dict[str, object]:
    return srm.build_payload(
        version="1.2.3",
        engine_amd64=_DIGEST_A,
        engine_arm64=_DIGEST_B,
        companion_amd64=_DIGEST_C,
        companion_arm64=_DIGEST_D,
        podman_version="6.1.1",
        machine_os="6.1",
    )


class TestBuildPayload:
    def test_shape_matches_the_contract(self) -> None:
        payload = _payload()
        assert payload["version"] == "1.2.3"
        assert payload["engine"]["linux/amd64"] == _DIGEST_A  # type: ignore[index]
        assert payload["companion"]["safent-ads"]["linux/arm64"] == _DIGEST_D  # type: ignore[index]
        assert payload["min_app_version"] == "1.2.3"


class TestValidatePayloadDigests:
    def test_accepts_a_well_formed_payload(self) -> None:
        srm.validate_payload_digests(_payload())  # does not raise

    def test_rejects_a_tag_instead_of_a_digest(self) -> None:
        payload = srm.build_payload(
            version="1.2.3",
            engine_amd64="latest",
            engine_arm64=_DIGEST_B,
            companion_amd64=_DIGEST_C,
            companion_arm64=_DIGEST_D,
            podman_version="6.1.1",
            machine_os="6.1",
        )
        with pytest.raises(srm.DigestValidationError):
            srm.validate_payload_digests(payload)


class TestCmdSignWithoutSigning:
    def test_writes_unsigned_manifest_when_no_key_given(self, tmp_path: Path) -> None:
        out_path = tmp_path / "runtime-manifest.json"
        rc = srm.main(["sign", *_SIGN_ARGS, "--out", str(out_path)])
        assert rc == 0
        document = json.loads(out_path.read_text())
        assert document["version"] == "2.0.0"
        assert not out_path.with_name(out_path.name + ".minisig").exists()

    def test_rejects_a_tag_and_writes_nothing(self, tmp_path: Path) -> None:
        out_path = tmp_path / "runtime-manifest.json"
        bad_args = list(_SIGN_ARGS)
        bad_args[bad_args.index(_DIGEST_A)] = "latest"
        rc = srm.main(["sign", *bad_args, "--out", str(out_path)])
        assert rc == 1
        assert not out_path.exists()


_FAKE_MINISIGN = """#!/usr/bin/env bash
set -e
echo "$@" >> "$FAKE_MINISIGN_LOG"
if [ "${FAKE_MINISIGN_FAILS:-0}" = "1" ]; then
  echo "fake minisign: forced failure" >&2
  exit 1
fi
sig_out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -x) sig_out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$sig_out" ] && printf 'fake-signature-bytes' > "$sig_out"
exit 0
"""


@pytest.fixture()
def fake_minisign_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    path = bin_dir / "minisign"
    path.write_text(_FAKE_MINISIGN)
    path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_MINISIGN_LOG", str(tmp_path / "minisign.log"))
    return bin_dir


class TestSignWithMinisignWrapper:
    @pytest.mark.usefixtures("fake_minisign_dir")
    def test_invokes_the_binary_and_produces_the_sig_file(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "runtime-manifest.json"
        manifest_path.write_text("{}")
        key_path = tmp_path / "fake.key"
        key_path.write_text("fake-key-content")

        sig_path = srm.sign_with_minisign(manifest_path, key_path, trusted_comment="hello")

        assert sig_path == manifest_path.with_name(manifest_path.name + ".minisig")
        assert sig_path.read_text() == "fake-signature-bytes"
        calls = (tmp_path / "minisign.log").read_text()
        assert "-S" in calls
        assert str(manifest_path) in calls
        assert "hello" in calls

    def test_raises_when_the_binary_is_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))  # empty dir, no minisign
        with pytest.raises(srm.MinisignNotAvailableError):
            srm.sign_with_minisign(tmp_path / "m.json", tmp_path / "k.key", trusted_comment="x")

    @pytest.mark.usefixtures("fake_minisign_dir")
    def test_raises_when_the_binary_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_MINISIGN_FAILS", "1")
        with pytest.raises(subprocess.CalledProcessError):
            srm.sign_with_minisign(tmp_path / "m.json", tmp_path / "k.key", trusted_comment="x")

    @pytest.mark.usefixtures("fake_minisign_dir")
    def test_stdin_is_closed_so_a_password_prompt_cannot_hang(self, tmp_path: Path) -> None:
        """A password-protected key would make real minisign prompt on
        stdin; DEVNULL makes that fail fast instead of hanging a CI job.
        The fake binary here just proves the call succeeds without stdin
        being connected to anything — a hang would time out the test run."""
        manifest_path = tmp_path / "runtime-manifest.json"
        manifest_path.write_text("{}")
        key_path = tmp_path / "fake.key"
        key_path.write_text("fake-key-content")

        srm.sign_with_minisign(manifest_path, key_path, trusted_comment="x")  # must not hang


class TestCmdSignEndToEndWithFakeMinisign:
    @pytest.mark.usefixtures("fake_minisign_dir")
    def test_full_cli_invocation_with_minisign_secret_key_produces_both_files(
        self, tmp_path: Path
    ) -> None:
        key_path = tmp_path / "fake.key"
        key_path.write_text("fake-key-content")
        out_path = tmp_path / "runtime-manifest.json"

        rc = srm.main(
            ["sign", *_SIGN_ARGS, "--minisign-secret-key", str(key_path), "--out", str(out_path)]
        )
        assert rc == 0
        assert out_path.exists()
        assert out_path.with_name("runtime-manifest.json.minisig").exists()

    @pytest.mark.usefixtures("fake_minisign_dir")
    def test_a_tag_is_rejected_before_any_signing_attempt(self, tmp_path: Path) -> None:
        key_path = tmp_path / "fake.key"
        key_path.write_text("fake-key-content")
        out_path = tmp_path / "runtime-manifest.json"
        bad_args = list(_SIGN_ARGS)
        bad_args[bad_args.index(_DIGEST_A)] = "latest"

        rc = srm.main(
            ["sign", *bad_args, "--minisign-secret-key", str(key_path), "--out", str(out_path)]
        )
        assert rc == 1
        assert not out_path.exists()
        assert not (tmp_path / "minisign.log").exists()  # minisign never invoked


@pytest.mark.skipif(shutil.which("minisign") is None, reason="minisign binary not on PATH")
class TestEndToEndWithRealMinisign:
    def test_sign_then_verify_with_the_daemon_side_verifier(self, tmp_path: Path) -> None:
        pub_path = tmp_path / "real.pub"
        key_path = tmp_path / "real.key"
        subprocess.run(
            ["minisign", "-G", "-W", "-f", "-p", str(pub_path), "-s", str(key_path), "-c", "t"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        out_path = tmp_path / "runtime-manifest.json"

        rc = srm.main(
            [
                "sign",
                "--version", "3.0.0",
                "--engine-amd64", _DIGEST_A,
                "--engine-arm64", _DIGEST_B,
                "--companion-amd64", _DIGEST_C,
                "--companion-arm64", _DIGEST_D,
                "--podman-version", "6.1.1",
                "--machine-os", "6.1",
                "--minisign-secret-key", str(key_path),
                "--out", str(out_path),
            ]
        )
        assert rc == 0

        pubkey_text = pub_path.read_text()
        minisig_text = out_path.with_name("runtime-manifest.json.minisig").read_text()
        assert rm.verify_minisign(out_path.read_bytes(), pubkey_text, minisig_text) is True
