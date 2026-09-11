"""GET /api/v1/system/update + runtime-manifest.json minisign verification
(T005/T006, contracts/update.md §2-3).

Three layers:
  - `hermes.shell_server.runtime_manifest` minisign wire-format parsing +
    verification — pure, no HTTP, no network. The bulk of these tests sign
    fixtures with a hand-built Python signer (`_minisign_sign` below) that
    mirrors the verifier's own parsing rules, so the suite never depends on
    the external `minisign` binary. `TestAgainstRealMinisignBinary` below
    additionally cross-checks against the REAL `minisign` 0.11 reference
    implementation when it is on PATH (skipped otherwise, same convention
    as tests/unit/ops/test_gitleaks_allowlist.py).
  - the HTTP route, via FastAPI's TestClient, network fetch monkeypatched.

The one invariant tasks.md calls out by name: a manifest whose signature
does not verify must produce `update_available: false`, even when the
plain-text VERSION file says a newer version exists — fail-closed per
Constitution Principle IV ("manifiesto sin firma valida -> sin boton").
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes
from hermes.shell_server import runtime_manifest as rm
from hermes.shell_server.system_update import create_system_update_router

pytestmark = pytest.mark.unit

_TOKEN = "test-bearer-token"  # noqa: S105 - test fixture, not a real credential
_KEY_ID = b"\x01\x02\x03\x04\x05\x06\x07\x08"
_OTHER_KEY_ID = b"\xff\xfe\xfd\xfc\xfb\xfa\xf9\xf8"


# ---------------------------------------------------------------------------
# Test-only minisign signer — mirrors runtime_manifest's verifier so the
# bulk of this suite is hermetic (no dependency on the external binary).
# ---------------------------------------------------------------------------


def _minisign_pubkey_text(public_key: Ed25519PublicKey, key_id: bytes = _KEY_ID) -> str:
    raw = b"Ed" + key_id + public_key.public_bytes_raw()
    return f"untrusted comment: test pubkey\n{base64.b64encode(raw).decode()}\n"


def _minisign_sign(
    file_bytes: bytes,
    private_key: Ed25519PrivateKey,
    *,
    key_id: bytes = _KEY_ID,
    trusted_comment: str = "test trusted comment",
    legacy: bool = False,
) -> str:
    alg = b"Ed" if legacy else b"ED"
    payload = file_bytes if legacy else hashlib.blake2b(file_bytes, digest_size=64).digest()
    sig = private_key.sign(payload)
    sig_raw = alg + key_id + sig
    global_sig = private_key.sign(sig + trusted_comment.encode("utf-8"))
    return (
        f"untrusted comment: test signature\n"
        f"{base64.b64encode(sig_raw).decode()}\n"
        f"trusted comment: {trusted_comment}\n"
        f"{base64.b64encode(global_sig).decode()}\n"
    )


def _generate_keypair(key_id: bytes = _KEY_ID) -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    return private_key, _minisign_pubkey_text(private_key.public_key(), key_id)


def _manifest_bytes(**overrides: object) -> bytes:
    import json

    payload: dict[str, object] = {
        "schema_version": 1,
        "version": "9.9.9",
        "engine": {"linux/amd64": "sha256:" + "1" * 64},
        "companion": {"safent-ads": {"linux/amd64": "sha256:" + "2" * 64}},
        "runtime_bundle": {"podman": "6.1.1", "machine_os": "6.1"},
        "min_app_version": "9.9.9",
    }
    payload.update(overrides)
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# minisign wire-format parsing + verification (pure)
# ---------------------------------------------------------------------------


class TestVerifyMinisign:
    def test_valid_signature_verifies(self) -> None:
        private_key, pubkey_text = _generate_keypair()
        file_bytes = _manifest_bytes()
        minisig_text = _minisign_sign(file_bytes, private_key)

        assert rm.verify_minisign(file_bytes, pubkey_text, minisig_text) is True

    def test_tampered_file_fails(self) -> None:
        private_key, pubkey_text = _generate_keypair()
        minisig_text = _minisign_sign(_manifest_bytes(), private_key)

        tampered = _manifest_bytes(version="0.0.1")
        assert rm.verify_minisign(tampered, pubkey_text, minisig_text) is False

    def test_tampered_trusted_comment_fails(self) -> None:
        """The global signature covers the trusted comment too — swapping
        it after signing must invalidate verification even though the
        main signature over the file itself is still technically valid."""
        private_key, pubkey_text = _generate_keypair()
        file_bytes = _manifest_bytes()
        minisig_text = _minisign_sign(file_bytes, private_key, trusted_comment="original")
        swapped = minisig_text.replace("trusted comment: original", "trusted comment: swapped")

        assert rm.verify_minisign(file_bytes, pubkey_text, swapped) is False

    def test_signed_by_the_wrong_key_fails(self) -> None:
        attacker_key, _ = _generate_keypair()
        _real_key, real_pubkey_text = _generate_keypair()
        file_bytes = _manifest_bytes()
        minisig_text = _minisign_sign(file_bytes, attacker_key)

        assert rm.verify_minisign(file_bytes, real_pubkey_text, minisig_text) is False

    def test_key_id_mismatch_fails_even_with_a_mathematically_valid_signature(self) -> None:
        """Belt-and-suspenders: if a signature's key id doesn't match the
        pubkey's, reject before even attempting Ed25519 verification."""
        private_key, pubkey_text = _generate_keypair(key_id=_KEY_ID)
        file_bytes = _manifest_bytes()
        minisig_text = _minisign_sign(file_bytes, private_key, key_id=_OTHER_KEY_ID)

        assert rm.verify_minisign(file_bytes, pubkey_text, minisig_text) is False

    def test_legacy_ed_mode_signature_is_rejected(self) -> None:
        """Only the prehashed 'ED' scheme is accepted (owner's spec) — a
        legacy 'Ed' (unhashed) signature, even a mathematically valid one
        over the same bytes, must not verify."""
        private_key, pubkey_text = _generate_keypair()
        file_bytes = _manifest_bytes()
        legacy_sig = _minisign_sign(file_bytes, private_key, legacy=True)

        assert rm.verify_minisign(file_bytes, pubkey_text, legacy_sig) is False

    def test_malformed_pubkey_text_fails_closed(self) -> None:
        private_key, _ = _generate_keypair()
        file_bytes = _manifest_bytes()
        minisig_text = _minisign_sign(file_bytes, private_key)

        assert rm.verify_minisign(file_bytes, "not a pubkey file", minisig_text) is False

    def test_malformed_minisig_text_fails_closed(self) -> None:
        _private_key, pubkey_text = _generate_keypair()
        assert rm.verify_minisign(_manifest_bytes(), pubkey_text, "not a minisig file") is False

    def test_does_not_raise_on_any_garbage_input(self) -> None:
        assert rm.verify_minisign(b"", "\x00\x01garbage", "\x00\x01garbage") is False


class TestIsPlaceholderPubkey:
    def test_the_committed_key_is_real_not_the_placeholder(self) -> None:
        text = (Path(rm._REPO_PUBKEY_PATH)).read_text()
        assert rm.is_placeholder_pubkey(text) is False, (
            "the committed key must be the real Safent updater key, not the placeholder"
        )
        assert "minisign public key: 242808B9F2E191FD" in text.splitlines()[0]

    def test_a_real_looking_key_is_not_flagged(self) -> None:
        _private_key, pubkey_text = _generate_keypair()
        assert rm.is_placeholder_pubkey(pubkey_text) is False

    def test_malformed_text_is_not_flagged_as_placeholder(self) -> None:
        """Malformed input fails verification elsewhere (fail-closed) — it
        is not specifically "the placeholder", a different failure mode."""
        assert rm.is_placeholder_pubkey("garbage") is False


class TestPiecesForArch:
    def test_lists_engine_and_companion_digests_for_the_requested_arch_only(self) -> None:
        manifest = rm.RuntimeManifest(
            version="1.0.0",
            engine={"linux/amd64": "sha256:aaa", "linux/arm64": "sha256:bbb"},
            companion={"safent-ads": {"linux/amd64": "sha256:ccc", "linux/arm64": "sha256:ddd"}},
        )
        pieces = rm.pieces_for_arch(manifest, "linux/amd64")
        assert {"kind": "engine", "digest": "sha256:aaa"} in pieces
        assert {"kind": "companion", "slug": "safent-ads", "digest": "sha256:ccc"} in pieces
        assert not any(p.get("digest") == "sha256:bbb" for p in pieces)

    def test_missing_arch_entry_is_omitted_not_fabricated(self) -> None:
        manifest = rm.RuntimeManifest(
            version="1.0.0", engine={"linux/amd64": "sha256:aaa"}, companion={}
        )
        pieces = rm.pieces_for_arch(manifest, "linux/arm64")
        assert pieces == []


# ---------------------------------------------------------------------------
# fetch_verified_manifest — orchestration
# ---------------------------------------------------------------------------


class TestFetchVerifiedManifest:
    def test_end_to_end_with_a_valid_signature(self, monkeypatch: pytest.MonkeyPatch) -> None:
        private_key, pubkey_text = _generate_keypair()
        file_bytes = _manifest_bytes(version="1.2.3")
        minisig_text = _minisign_sign(file_bytes, private_key)

        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", pubkey_text)
        monkeypatch.setattr(rm, "_fetch_raw_bytes", lambda: file_bytes)
        monkeypatch.setattr(rm, "_fetch_text", lambda _url: minisig_text)

        manifest = rm.fetch_verified_manifest()
        assert manifest is not None
        assert manifest.version == "1.2.3"

    def test_no_pubkey_configured_anywhere_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", "")
        monkeypatch.setattr(rm, "_BAKED_PUBKEY_PATH", Path("/does/not/exist"))
        monkeypatch.setattr(rm, "_REPO_PUBKEY_PATH", Path("/does/not/exist/either"))

        assert rm.fetch_verified_manifest() is None

    def test_default_resolution_finds_the_repo_placeholder_and_stays_unverified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No override, no baked container file (this is a bare checkout) —
        falls back to ops/keys/runtime-manifest.pub, which today IS the
        placeholder, so nothing ever verifies until T023 fills it in."""
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", "")
        monkeypatch.setattr(rm, "_BAKED_PUBKEY_PATH", Path("/does/not/exist"))

        assert rm.fetch_verified_manifest() is None

    def test_fetch_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _key, pubkey_text = _generate_keypair()
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", pubkey_text)
        monkeypatch.setattr(rm, "_fetch_raw_bytes", lambda: None)
        monkeypatch.setattr(rm, "_fetch_text", lambda _url: "irrelevant")

        assert rm.fetch_verified_manifest() is None

    def test_missing_minisig_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _key, pubkey_text = _generate_keypair()
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", pubkey_text)
        monkeypatch.setattr(rm, "_fetch_raw_bytes", _manifest_bytes)
        monkeypatch.setattr(rm, "_fetch_text", lambda _url: None)

        assert rm.fetch_verified_manifest() is None

    def test_tampered_manifest_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        private_key, pubkey_text = _generate_keypair()
        signed_bytes = _manifest_bytes(version="1.2.3")
        minisig_text = _minisign_sign(signed_bytes, private_key)

        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", pubkey_text)
        monkeypatch.setattr(rm, "_fetch_raw_bytes", lambda: _manifest_bytes(version="9.9.9"))
        monkeypatch.setattr(rm, "_fetch_text", lambda _url: minisig_text)

        assert rm.fetch_verified_manifest() is None

    def test_invalid_json_after_a_valid_signature_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defense in depth: even if something signed non-JSON bytes, the
        daemon must not crash trying to parse them."""
        private_key, pubkey_text = _generate_keypair()
        not_json = b"not actually json"
        minisig_text = _minisign_sign(not_json, private_key)

        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", pubkey_text)
        monkeypatch.setattr(rm, "_fetch_raw_bytes", lambda: not_json)
        monkeypatch.setattr(rm, "_fetch_text", lambda _url: minisig_text)

        assert rm.fetch_verified_manifest() is None


# ---------------------------------------------------------------------------
# Cross-check against the REAL minisign binary (skipped if not on PATH)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("minisign") is None, reason="minisign binary not on PATH")
class TestAgainstRealMinisignBinary:
    def test_a_signature_produced_by_real_minisign_verifies(self, tmp_path: Path) -> None:
        pub_path = tmp_path / "real.pub"
        key_path = tmp_path / "real.key"
        subprocess.run(
            ["minisign", "-G", "-W", "-f", "-p", str(pub_path), "-s", str(key_path), "-c", "t"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        manifest_path = tmp_path / "runtime-manifest.json"
        manifest_path.write_bytes(_manifest_bytes(version="5.5.5"))
        subprocess.run(
            ["minisign", "-S", "-s", str(key_path), "-m", str(manifest_path), "-t", "real"],
            check=True,
            capture_output=True,
            timeout=10,
        )

        pubkey_text = pub_path.read_text()
        minisig_text = (tmp_path / "runtime-manifest.json.minisig").read_text()
        assert rm.verify_minisign(manifest_path.read_bytes(), pubkey_text, minisig_text) is True

    def test_real_minisign_rejects_what_we_tampered_and_so_do_we(self, tmp_path: Path) -> None:
        pub_path = tmp_path / "real.pub"
        key_path = tmp_path / "real.key"
        subprocess.run(
            ["minisign", "-G", "-W", "-f", "-p", str(pub_path), "-s", str(key_path), "-c", "t"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        manifest_path = tmp_path / "runtime-manifest.json"
        manifest_path.write_bytes(_manifest_bytes(version="5.5.5"))
        subprocess.run(
            ["minisign", "-S", "-s", str(key_path), "-m", str(manifest_path), "-t", "real"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        manifest_path.write_bytes(_manifest_bytes(version="6.6.6"))  # tamper after signing

        real_verify = subprocess.run(
            ["minisign", "-V", "-p", str(pub_path), "-m", str(manifest_path)],
            capture_output=True,
            timeout=10,
            check=False,  # non-zero IS the expected outcome
        )
        assert real_verify.returncode != 0

        pubkey_text = pub_path.read_text()
        minisig_text = (tmp_path / "runtime-manifest.json.minisig").read_text()
        assert rm.verify_minisign(manifest_path.read_bytes(), pubkey_text, minisig_text) is False


class TestAgainstRsign2TauriFormat:
    """The release signer will be the Tauri CLI signer (rsign2), not the C
    `minisign` binary above — Tauri's key is unencrypted, which the C
    binary cannot load, so the pipeline signs with the Rust toolchain
    instead. Cross-lane check (coordinator, on top of app-contracts-cli):
    prove `verify_minisign` accepts what THAT toolchain actually produces,
    not just the C reference implementation.

    `npx tauri` / `cargo tauri` were not available in this sandbox
    (desktop/ has no installed tauri-cli), so per the fallback this test
    vectors against a fixture vendored from `rsign2` instead — the
    standalone Rust CLI (github.com/jedisct1/rsign2) built on the same
    `minisign` Rust crate Tauri's own signer uses; `rsign -H` is a no-op
    "kept for backwards compatibility", i.e. rsign2 always produces the
    prehashed "ED" scheme, same as the fixture's algorithm bytes below.

    Fixture provenance (tests/unit/agents_os/fixtures/rsign2_*):
      $ cargo install rsign2          # 0.6.6, minisign crate 0.9.1
      $ rsign generate -f -W --unencrypted -p rsign.pub -s rsign.key \
            -c "signature from tauri secret key"
      $ rsign sign -s rsign.key -p rsign.pub -W \
            -x runtime-manifest.json.sig \
            -c "signature from tauri secret key" \
            -t "timestamp:1789062638	file:runtime-manifest.json" \
            runtime-manifest.json
    The untrusted comment ("signature from tauri secret key") and the
    tab-separated trusted comment (`timestamp:...\tfile:...`) match
    exactly what the coordinator specified Tauri produces. The key is
    throwaway, generated only for this fixture, discarded after use.
    """

    _FIXTURES = Path(__file__).parent / "fixtures"

    def test_verifies_a_real_rsign2_signature_with_tauri_style_comments(self) -> None:
        pubkey_text = (self._FIXTURES / "rsign2_runtime_manifest.pub").read_text()
        minisig_text = (self._FIXTURES / "rsign2_runtime_manifest.json.sig").read_text()
        file_bytes = (self._FIXTURES / "rsign2_runtime_manifest.json").read_bytes()

        assert rm.verify_minisign(file_bytes, pubkey_text, minisig_text) is True

    def test_the_fixture_really_does_use_the_documented_comment_conventions(self) -> None:
        """Guards the fixture itself against silent drift/corruption."""
        pubkey_text = (self._FIXTURES / "rsign2_runtime_manifest.pub").read_text()
        minisig_text = (self._FIXTURES / "rsign2_runtime_manifest.json.sig").read_text()

        pubkey_comment = pubkey_text.splitlines()[0]
        assert pubkey_comment == "untrusted comment: minisign public key: C2FF950EFBAC14E0"
        assert minisig_text.splitlines()[0] == "untrusted comment: signature from tauri secret key"
        trusted = minisig_text.splitlines()[2]
        assert trusted.startswith("trusted comment: timestamp:")
        assert "\tfile:runtime-manifest.json" in trusted

        parsed = rm._parse_minisig(minisig_text)
        assert parsed is not None
        assert parsed.trusted_comment.startswith("timestamp:")
        assert "\tfile:runtime-manifest.json" in parsed.trusted_comment

    def test_tampering_the_fixture_file_after_the_fact_fails_closed(self) -> None:
        pubkey_text = (self._FIXTURES / "rsign2_runtime_manifest.pub").read_text()
        minisig_text = (self._FIXTURES / "rsign2_runtime_manifest.json.sig").read_text()
        file_bytes = (self._FIXTURES / "rsign2_runtime_manifest.json").read_bytes()

        assert rm.verify_minisign(file_bytes + b" ", pubkey_text, minisig_text) is False


# ---------------------------------------------------------------------------
# HTTP route
# ---------------------------------------------------------------------------


def _client() -> TestClient:
    app = FastAPI()
    app.state.shell_webui_token = _TOKEN
    app.include_router(create_system_update_router())
    return TestClient(app)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


class TestGetSystemUpdateAuth:
    def test_missing_bearer_is_401(self) -> None:
        r = _client().get("/api/v1/system/update")
        assert r.status_code == 401

    def test_wrong_bearer_is_401(self) -> None:
        r = _client().get("/api/v1/system/update", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401


class TestGetSystemUpdateShape:
    def test_existing_fields_are_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import hermes.shell_server.system_update as su

        monkeypatch.setattr(su, "_fetch_latest", lambda: None)
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", "")
        monkeypatch.setattr(rm, "_BAKED_PUBKEY_PATH", Path("/does/not/exist"))
        monkeypatch.setattr(rm, "_REPO_PUBKEY_PATH", Path("/does/not/exist/either"))

        r = _client().get("/api/v1/system/update", headers=_auth_headers())
        assert r.status_code == 200
        body = r.json()
        for key in ("current_version", "latest_version", "update_available", "updating"):
            assert key in body

    def test_new_fields_are_present_and_null_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes.shell_server.system_update as su

        monkeypatch.setattr(su, "_fetch_latest", lambda: None)
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", "")
        monkeypatch.setattr(rm, "_BAKED_PUBKEY_PATH", Path("/does/not/exist"))
        monkeypatch.setattr(rm, "_REPO_PUBKEY_PATH", Path("/does/not/exist/either"))

        body = _client().get("/api/v1/system/update", headers=_auth_headers()).json()
        assert body["engine_digest"] is None
        assert body["companion_digest"] is None
        assert body["pieces"] == []


class TestGetSystemUpdateContractV3Fields:
    """UPD-N2 (specs/025-safent-repaso matriz-final-39eeb8e): contracts/
    update.md §3 says the route "se amplía con los mismos campos" del objeto
    `window.__safentUpdate` — `available`, `current` (VersionSet), `to`,
    `checked_at` — CONSERVING `current_version`/`latest_version`/
    `update_available`. Before this fix only `pieces`/`engine_digest`/
    `companion_digest` had been added; these four were missing entirely."""

    def test_available_mirrors_update_available_when_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes.shell_server.system_update as su

        monkeypatch.setattr(su, "_fetch_latest", lambda: None)
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", "")
        monkeypatch.setattr(rm, "_BAKED_PUBKEY_PATH", Path("/does/not/exist"))
        monkeypatch.setattr(rm, "_REPO_PUBKEY_PATH", Path("/does/not/exist/either"))

        body = _client().get("/api/v1/system/update", headers=_auth_headers()).json()
        assert body["available"] is False
        assert body["available"] == body["update_available"]
        assert body["to"] is None

    def test_current_is_a_version_set_with_the_running_app_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes.shell_server.system_update as su

        monkeypatch.setattr(su, "_fetch_latest", lambda: None)
        monkeypatch.setattr(hermes, "__version__", "0.8.42", raising=False)
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", "")
        monkeypatch.setattr(rm, "_BAKED_PUBKEY_PATH", Path("/does/not/exist"))
        monkeypatch.setattr(rm, "_REPO_PUBKEY_PATH", Path("/does/not/exist/either"))

        body = _client().get("/api/v1/system/update", headers=_auth_headers()).json()
        assert body["current"]["app"] == "0.8.42"
        assert isinstance(
            body["current"]["engine"], str
        )  # never null — VersionSet.engine is non-nullable
        assert body["current"]["companion"] is None

    def test_checked_at_is_a_fresh_iso_timestamp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import hermes.shell_server.system_update as su

        monkeypatch.setattr(su, "_fetch_latest", lambda: None)
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", "")
        monkeypatch.setattr(rm, "_BAKED_PUBKEY_PATH", Path("/does/not/exist"))
        monkeypatch.setattr(rm, "_REPO_PUBKEY_PATH", Path("/does/not/exist/either"))

        before = datetime.now(tz=UTC)
        body = _client().get("/api/v1/system/update", headers=_auth_headers()).json()
        after = datetime.now(tz=UTC)

        checked_at = datetime.fromisoformat(body["checked_at"])
        assert before <= checked_at <= after

    def test_to_is_populated_with_the_target_version_set_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same fixture as the existing "flips the button on" test — pins
        the NEW `available`/`current`/`to` fields on the identical scenario."""
        import hermes.shell_server.system_update as su

        private_key, pubkey_text = _generate_keypair()
        file_bytes = _manifest_bytes(
            version="999.0.0",
            engine={"linux/amd64": "sha256:" + "a" * 64},
            companion={"safent-ads": {"linux/amd64": "sha256:" + "b" * 64}},
        )
        minisig_text = _minisign_sign(file_bytes, private_key)

        monkeypatch.setattr(su, "_fetch_latest", lambda: "999.0.0")
        monkeypatch.setattr(hermes, "__version__", "0.1.0", raising=False)
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", pubkey_text)
        monkeypatch.setattr(rm, "_fetch_raw_bytes", lambda: file_bytes)
        monkeypatch.setattr(rm, "_fetch_text", lambda _url: minisig_text)
        monkeypatch.setattr(rm, "current_arch_key", lambda: "linux/amd64")
        monkeypatch.setattr(su, "current_arch_key", lambda: "linux/amd64")

        body = _client().get("/api/v1/system/update", headers=_auth_headers()).json()
        assert body["available"] is True
        assert body["current"] == {
            "app": "0.1.0",
            "engine": body["current"]["engine"],
            "companion": None,
        }
        assert body["to"] == {
            "app": "999.0.0",
            "engine": "sha256:" + "a" * 64,
            "companion": "sha256:" + "b" * 64,
        }


class TestUpdateAvailableIsFailClosedOnManifestSignature:
    def test_unsigned_manifest_means_no_button_even_if_version_text_is_newer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE T005 test tasks.md calls out by name: a newer plain-text
        VERSION alone must never flip update_available to true."""
        import hermes.shell_server.system_update as su

        monkeypatch.setattr(su, "_fetch_latest", lambda: "999.0.0")
        monkeypatch.setattr(hermes, "__version__", "0.1.0", raising=False)
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", "")
        monkeypatch.setattr(rm, "_BAKED_PUBKEY_PATH", Path("/does/not/exist"))
        monkeypatch.setattr(rm, "_REPO_PUBKEY_PATH", Path("/does/not/exist/either"))

        body = _client().get("/api/v1/system/update", headers=_auth_headers()).json()
        assert body["update_available"] is False
        assert body["engine_digest"] is None

    def test_tampered_manifest_means_no_button(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import hermes.shell_server.system_update as su

        private_key, pubkey_text = _generate_keypair()
        signed_bytes = _manifest_bytes(version="999.0.0")
        minisig_text = _minisign_sign(signed_bytes, private_key)

        monkeypatch.setattr(su, "_fetch_latest", lambda: "999.0.0")
        monkeypatch.setattr(hermes, "__version__", "0.1.0", raising=False)
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", pubkey_text)
        monkeypatch.setattr(rm, "_fetch_raw_bytes", lambda: _manifest_bytes(version="9.9.9"))
        monkeypatch.setattr(rm, "_fetch_text", lambda _url: minisig_text)

        body = _client().get("/api/v1/system/update", headers=_auth_headers()).json()
        assert body["update_available"] is False

    def test_valid_signed_manifest_with_a_newer_version_flips_the_button_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes.shell_server.system_update as su

        private_key, pubkey_text = _generate_keypair()
        file_bytes = _manifest_bytes(
            version="999.0.0",
            engine={"linux/amd64": "sha256:" + "a" * 64},
            companion={"safent-ads": {"linux/amd64": "sha256:" + "b" * 64}},
        )
        minisig_text = _minisign_sign(file_bytes, private_key)

        monkeypatch.setattr(su, "_fetch_latest", lambda: "999.0.0")
        monkeypatch.setattr(hermes, "__version__", "0.1.0", raising=False)
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", pubkey_text)
        monkeypatch.setattr(rm, "_fetch_raw_bytes", lambda: file_bytes)
        monkeypatch.setattr(rm, "_fetch_text", lambda _url: minisig_text)
        monkeypatch.setattr(rm, "current_arch_key", lambda: "linux/amd64")
        monkeypatch.setattr(su, "current_arch_key", lambda: "linux/amd64")

        body = _client().get("/api/v1/system/update", headers=_auth_headers()).json()
        assert body["update_available"] is True
        assert body["engine_digest"] == "sha256:" + "a" * 64
        assert body["companion_digest"] == "sha256:" + "b" * 64
        assert {"kind": "engine", "digest": "sha256:" + "a" * 64} in body["pieces"]

    def test_same_version_text_with_a_valid_manifest_stays_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Digest-vs-version nuance lives in the wrapper (RT-DESK); this
        endpoint's own gate is still the app semver comparison — a valid
        manifest alone, with no version bump, must not flip the button."""
        import hermes.shell_server.system_update as su

        private_key, pubkey_text = _generate_keypair()
        file_bytes = _manifest_bytes(version="0.1.0")
        minisig_text = _minisign_sign(file_bytes, private_key)

        monkeypatch.setattr(su, "_fetch_latest", lambda: "0.1.0")
        monkeypatch.setattr(hermes, "__version__", "0.1.0", raising=False)
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", pubkey_text)
        monkeypatch.setattr(rm, "_fetch_raw_bytes", lambda: file_bytes)
        monkeypatch.setattr(rm, "_fetch_text", lambda _url: minisig_text)

        body = _client().get("/api/v1/system/update", headers=_auth_headers()).json()
        assert body["update_available"] is False


class TestUpdatingReflectsInstallRequests:
    """T006 extracted the marker mechanism to install_requests.py; `updating`
    must still track it exactly via the shared `is_verb_live` read."""

    def test_false_with_no_live_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import hermes.shell_server.system_update as su

        monkeypatch.setattr(su, "_fetch_latest", lambda: None)
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", "")
        monkeypatch.setattr(rm, "_BAKED_PUBKEY_PATH", Path("/does/not/exist"))
        monkeypatch.setattr(rm, "_REPO_PUBKEY_PATH", Path("/does/not/exist/either"))

        body = _client().get("/api/v1/system/update", headers=_auth_headers()).json()
        assert body["updating"] is False

    def test_true_once_an_update_system_request_is_live(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import hermes.shell_server.install_requests as ir
        import hermes.shell_server.system_update as su

        monkeypatch.setattr(ir, "_INSTANCE_DIR", tmp_path / "instance")
        monkeypatch.setattr(su, "_fetch_latest", lambda: None)
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", "")
        monkeypatch.setattr(rm, "_BAKED_PUBKEY_PATH", Path("/does/not/exist"))
        monkeypatch.setattr(rm, "_REPO_PUBKEY_PATH", Path("/does/not/exist/either"))

        ir.create_request("update_system")

        body = _client().get("/api/v1/system/update", headers=_auth_headers()).json()
        assert body["updating"] is True


class TestSignedReleaseIdentity:
    @pytest.mark.parametrize(
        "version,engine,companion",
        [
            ("9.9.9", {"linux/amd64": "sha256:" + "a" * 64}, {}),
            ("999.0.0", {"linux/arm64": "sha256:" + "a" * 64}, {}),
            ("999.0.0", {"linux/amd64": "not-a-digest"}, {}),
            (
                "999.0.0",
                {"linux/amd64": "sha256:" + "a" * 64},
                {"safent-ads": {"linux/amd64": "sha256:short"}},
            ),
        ],
    )
    def test_valid_signature_does_not_authorize_incoherent_target(
        self,
        monkeypatch,
        version,
        engine,
        companion,
    ):
        import hermes.shell_server.system_update as su

        key, public = _generate_keypair()
        body = _manifest_bytes(version=version, engine=engine, companion=companion)
        signature = _minisign_sign(body, key)
        monkeypatch.setattr(su, "_fetch_latest", lambda: "999.0.0")
        monkeypatch.setattr(hermes, "__version__", "0.1.0")
        monkeypatch.setattr(su, "current_arch_key", lambda: "linux/amd64")
        monkeypatch.setattr(rm, "_PUBKEY_TEXT_OVERRIDE", public)
        monkeypatch.setattr(rm, "_fetch_raw_bytes", lambda: body)
        monkeypatch.setattr(rm, "_fetch_text", lambda _url: signature)
        result = _client().get("/api/v1/system/update", headers=_auth_headers()).json()
        assert result["update_available"] is False
        assert result["available"] is False
        assert result["to"] is None

    @pytest.mark.parametrize(
        "value",
        ["", "garbage999", "1.2", "1.2.3.4", "01.2.3", "1.2.3-01", "1.2.3-", "1.2.3\n", "v" * 200],
    )
    def test_malformed_semver_has_no_order(self, value):
        from hermes.shell_server.system_update import _parse

        assert _parse(value) is None

    def test_prerelease_semantics_and_build_metadata(self):
        from hermes.shell_server.system_update import _parse

        ordered = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
            "1.0.1",
        ]
        assert all(_parse(a) < _parse(b) for a, b in zip(ordered, ordered[1:], strict=False))
        assert _parse("1.0.0+build.3") == _parse("v1.0.0+build.2")

    def test_unsigned_version_fetch_is_size_bounded(self, monkeypatch):
        import io

        import hermes.shell_server.system_update as su

        payload = io.BytesIO(b"9" * 1000)

        class Response:
            def __enter__(self):
                return payload

            def __exit__(self, *_args):
                return None

        monkeypatch.setattr(su.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
        assert su._fetch_latest() is None
        assert payload.tell() == su._MAX_VERSION_BYTES + 1

    def test_network_checks_run_outside_request_event_loop(self, monkeypatch):
        import asyncio

        import hermes.shell_server.system_update as su

        checked = []

        def in_worker():
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            checked.append(True)

        monkeypatch.setattr(su, "_fetch_latest", in_worker)
        monkeypatch.setattr(su, "fetch_verified_manifest", in_worker)
        response = _client().get("/api/v1/system/update", headers=_auth_headers())
        assert response.status_code == 200
        assert checked == [True, True]
