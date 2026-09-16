"""runtime_manifest — fetch + verify the signed `runtime-manifest.json`
(contracts/update.md §2-3, T005/T006).

This is what turns "there might be a new version" into a fact the daemon can
act on: the engine/companion digests currently published, signed so a
compromised or spoofed CDN response can never make the update footer light
up. FAIL-CLOSED by construction (Constitution Principle IV — "manifiesto sin
firma valida -> sin boton"): any failure (network, malformed JSON, missing
or invalid signature, no public key configured) returns None, and the
caller MUST treat None as "nothing verified", never as "nothing new".

Signing: minisign, prehashed "ED" mode (BLAKE2b-512 + Ed25519), the SAME
key as the Tauri updater's `latest.json` (owner's decision — one key for
both, TAURI_SIGNING_PRIVATE_KEY in agents-autonomy). Supersedes T005's
original Ed25519-hex scheme (hermes.config_sync.signature.verify_bundle),
which was a documented interim deviation; contracts/update.md §2 has been
corrected back to its original text. No minisign LIBRARY exists for Python
in this repo's dependencies, so verification is implemented directly on
`cryptography`'s Ed25519 primitive + stdlib `hashlib.blake2b` — no new
dependency. The wire format below is verified against the real `minisign`
0.11 reference implementation (round-trip tests), not derived from memory
alone.

Public key: committed at `ops/keys/runtime-manifest.pub` (minisign public
key file format), baked into the image at
`/usr/share/hermes/keys/runtime-manifest.pub` (Containerfile). This is a
PLACEHOLDER (all-zero key) until the agents-autonomy publish pipeline
(T023) generates the real pair and commits the public half — see that
file's own comment and `is_placeholder_pubkey()` below.
`SAFENT_RUNTIME_MANIFEST_PUBKEY`, when set, overrides with literal
`.pub`-file text — for tests only; production reads the baked file.

The PRIVATE key never enters this repository or this module — it lives in
release tooling / CI (ops/container/sign_runtime_manifest.py, or the real
`minisign` CLI run directly by the publish pipeline).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import urllib.request
from base64 import b64decode
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger("hermes.shell_server.runtime_manifest")

_MANIFEST_URL = os.environ.get(
    "SAFENT_RUNTIME_MANIFEST_URL",
    "https://raw.githubusercontent.com/devwspito/safent-runtime/main/runtime-manifest.json",
)
_MINISIG_URL = _MANIFEST_URL + ".minisig"

# Test-only override: literal minisign .pub file text. Production always
# reads the baked file (see _resolve_pubkey_text).
_PUBKEY_TEXT_OVERRIDE = os.environ.get("SAFENT_RUNTIME_MANIFEST_PUBKEY", "")

_BAKED_PUBKEY_PATH = Path("/usr/share/hermes/keys/runtime-manifest.pub")
# Repo-relative fallback for running the daemon/tests directly on a host
# checkout (no container) — src/hermes/shell_server/ -> repo root.
_REPO_PUBKEY_PATH = Path(__file__).resolve().parents[3] / "ops" / "keys" / "runtime-manifest.pub"

_MINISIGN_PUBKEY_ALG = b"Ed"
_MINISIGN_SIG_ALG_PREHASHED = b"ED"
_MINISIGN_PUBKEY_LEN = 42  # alg(2) + keyid(8) + pubkey(32)
_MINISIGN_SIG_LEN = 74  # alg(2) + keyid(8) + signature(64)
_PUBKEY_FILE_MIN_LINES = 2  # untrusted comment + base64
_MINISIG_FILE_MIN_LINES = 4  # untrusted comment, sig, trusted comment, global sig


@dataclass(frozen=True)
class RuntimeManifest:
    """Verified payload of runtime-manifest.json (contracts/update.md §2)."""

    version: str
    engine: dict[str, str] = field(default_factory=dict)
    companion: dict[str, dict[str, str]] = field(default_factory=dict)
    min_app_version: str = ""


def current_arch_key() -> str:
    """The `os/arch` key this manifest indexes digests by, for THIS host."""
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    return f"linux/{arch}"


# ---------------------------------------------------------------------------
# minisign wire format — parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ParsedPubkey:
    key_id: bytes
    public_key: bytes


@dataclass(frozen=True)
class _ParsedMinisig:
    key_id: bytes
    signature: bytes
    trusted_comment: str
    global_signature: bytes


def _parse_minisign_pubkey(text: str) -> _ParsedPubkey | None:
    lines = text.splitlines()
    if len(lines) < _PUBKEY_FILE_MIN_LINES or not lines[0].startswith("untrusted comment:"):
        return None
    try:
        raw = b64decode(lines[1], validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) != _MINISIGN_PUBKEY_LEN or raw[0:2] != _MINISIGN_PUBKEY_ALG:
        return None
    return _ParsedPubkey(key_id=raw[2:10], public_key=raw[10:42])


def _parse_minisig(text: str) -> _ParsedMinisig | None:
    lines = text.splitlines()
    if len(lines) < _MINISIG_FILE_MIN_LINES:
        return None
    if not (lines[0].startswith("untrusted comment:") and lines[2].startswith("trusted comment:")):
        return None
    try:
        sig_raw = b64decode(lines[1], validate=True)
        global_sig = b64decode(lines[3], validate=True)
    except (ValueError, TypeError):
        return None
    if len(sig_raw) != _MINISIGN_SIG_LEN or sig_raw[0:2] != _MINISIGN_SIG_ALG_PREHASHED:
        return None
    trusted_comment = lines[2][len("trusted comment:") :].lstrip(" ")
    return _ParsedMinisig(
        key_id=sig_raw[2:10],
        signature=sig_raw[10:74],
        trusted_comment=trusted_comment,
        global_signature=global_sig,
    )


def is_placeholder_pubkey(pubkey_text: str) -> bool:
    """True if `pubkey_text` is the checked-in all-zero placeholder — the
    publish pipeline (T023) has not yet committed the real key."""
    parsed = _parse_minisign_pubkey(pubkey_text)
    if parsed is None:
        return False
    return parsed.public_key == b"\x00" * 32


# ---------------------------------------------------------------------------
# minisign wire format — verification
# ---------------------------------------------------------------------------


def verify_minisign(file_bytes: bytes, pubkey_text: str, minisig_text: str) -> bool:
    """Verify `file_bytes` against a detached minisign signature.

    Only the prehashed "ED" scheme (BLAKE2b-512 + Ed25519) is accepted —
    legacy "Ed" (unhashed) signatures are rejected, matching what `minisign
    -S` produces by default. Fail-closed: any parse error, algorithm
    mismatch, key-id mismatch, or invalid signature returns False; no
    exception ever escapes this function.
    """
    try:
        pubkey = _parse_minisign_pubkey(pubkey_text)
        minisig = _parse_minisig(minisig_text)
        if pubkey is None or minisig is None:
            logger.warning("hermes.runtime_manifest.minisign_parse_failed")
            return False
        if minisig.key_id != pubkey.key_id:
            logger.warning("hermes.runtime_manifest.minisign_keyid_mismatch")
            return False

        verifier = Ed25519PublicKey.from_public_bytes(pubkey.public_key)
        digest = hashlib.blake2b(file_bytes, digest_size=64).digest()
        verifier.verify(minisig.signature, digest)
        verifier.verify(
            minisig.global_signature,
            minisig.signature + minisig.trusted_comment.encode("utf-8"),
        )
        return True
    except InvalidSignature:
        logger.warning("hermes.runtime_manifest.minisign_signature_invalid")
        return False
    except Exception:  # noqa: BLE001 - fail-closed on ANY malformed input
        logger.warning("hermes.runtime_manifest.minisign_verify_error")
        return False


# ---------------------------------------------------------------------------
# Fetch + assemble
# ---------------------------------------------------------------------------


def _resolve_pubkey_text() -> str | None:
    if _PUBKEY_TEXT_OVERRIDE:
        return _PUBKEY_TEXT_OVERRIDE
    for path in (_BAKED_PUBKEY_PATH, _REPO_PUBKEY_PATH):
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return None


def _fetch_text(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - fixed, non-user-controlled URL
            body: bytes = r.read()
    except Exception:  # noqa: BLE001 - network is best-effort, caller fails closed
        return None
    return body.decode("utf-8")


def _fetch_raw_bytes() -> bytes | None:
    try:
        with urllib.request.urlopen(_MANIFEST_URL, timeout=5) as r:  # noqa: S310
            return bytes(r.read())
    except Exception:  # noqa: BLE001 - network is best-effort, caller fails closed
        return None


def _as_str_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _as_nested_str_dict(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    return {str(k): _as_str_dict(v) for k, v in value.items()}


def _parse_payload(payload: dict[str, object]) -> RuntimeManifest | None:
    try:
        return RuntimeManifest(
            version=str(payload["version"]),
            engine=_as_str_dict(payload.get("engine")),
            companion=_as_nested_str_dict(payload.get("companion")),
            min_app_version=str(payload.get("min_app_version") or payload["version"]),
        )
    except (KeyError, TypeError) as exc:
        logger.warning("hermes.runtime_manifest.malformed: %s", type(exc).__name__)
        return None


def _usable_pubkey_text() -> str | None:
    """The pubkey text to verify with, or None if unconfigured/placeholder."""
    pubkey_text = _resolve_pubkey_text()
    if pubkey_text is None:
        logger.warning("hermes.runtime_manifest.pubkey_not_configured")
        return None
    if is_placeholder_pubkey(pubkey_text):
        logger.warning("hermes.runtime_manifest.pubkey_is_placeholder")
        return None
    return pubkey_text


def _parse_json_dict(file_bytes: bytes) -> dict[str, object] | None:
    try:
        payload = json.loads(file_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.warning("hermes.runtime_manifest.invalid_json")
        return None
    return payload if isinstance(payload, dict) else None


def fetch_verified_manifest() -> RuntimeManifest | None:
    """Fetch + verify runtime-manifest.json (+ .minisig). None on ANY
    failure (fail-closed) — network, missing/placeholder key, bad
    signature, or malformed JSON."""
    pubkey_text = _usable_pubkey_text()
    if pubkey_text is None:
        return None

    file_bytes = _fetch_raw_bytes()
    minisig_text = _fetch_text(_MINISIG_URL)
    if file_bytes is None or minisig_text is None:
        return None
    if not verify_minisign(file_bytes, pubkey_text, minisig_text):
        return None

    payload = _parse_json_dict(file_bytes)
    return _parse_payload(payload) if payload is not None else None


def pieces_for_arch(manifest: RuntimeManifest, arch_key: str) -> list[dict[str, str]]:
    """The published pieces relevant to THIS host's architecture — informational
    for the UI/wrapper (the wrapper is the one that knows what is actually
    running and therefore what is genuinely newer, via `safent facts --json`)."""
    pieces: list[dict[str, str]] = []
    engine_digest = manifest.engine.get(arch_key)
    if engine_digest:
        pieces.append({"kind": "engine", "digest": engine_digest})
    for slug, per_arch in manifest.companion.items():
        digest = per_arch.get(arch_key)
        if digest:
            pieces.append({"kind": "companion", "slug": slug, "digest": digest})
    return pieces
