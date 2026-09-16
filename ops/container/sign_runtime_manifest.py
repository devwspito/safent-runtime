#!/usr/bin/env python3
"""sign_runtime_manifest.py — release tooling for runtime-manifest.json
(contracts/update.md §2, T005/T006). The daemon-side counterpart that
fetches and verifies this file + its `.minisig` is
`hermes.shell_server.runtime_manifest`.

One subcommand:
  sign   assemble runtime-manifest.json from explicit per-arch digests
         (must already be resolved by the publish pipeline's OWN registry
         tooling — e.g. `docker buildx imagetools inspect`, T023 — this
         script never talks to a registry itself, so it stays a pure,
         fully unit-tested function with no network dependency), then
         optionally sign it.

Key handling (owner's decision — ONE minisign key for both this file and
the Tauri updater's latest.json): key generation and secret-key handling
are NOT reimplemented here — that is exactly the part a hand-rolled
crypto-file parser should not touch. Two ways to produce the `.minisig`:

  1. `--minisign-secret-key <file>` — this script shells out to the real
     `minisign` binary (must be on PATH): `minisign -S -s <file> -m <out>
     -x <out>.minisig`. Use an UNENCRYPTED key (`minisign -G -W`, the
     standard convention for a CI/automation credential stored encrypted
     at rest by the secret store instead) — stdin is closed, so a
     password-protected key fails fast instead of hanging the job.
  2. Omit the flag: this script writes ONLY the unsigned
     runtime-manifest.json; the pipeline signs it itself directly with
     `minisign -S -s <key> -m runtime-manifest.json`.

The key itself is generated once with `minisign -G` and stored as a CI
secret (mirrors TAURI_SIGNING_PRIVATE_KEY, already used by the desktop
pipeline in agents-autonomy/.github/workflows) — never committed. The
PUBLIC key (`ops/keys/runtime-manifest.pub`) is not secret and is checked
into this repo.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

_REQUIRED_DIGEST_PREFIX = "sha256:"
_MINISIGN_TIMEOUT_S = 30


def build_payload(
    *,
    version: str,
    engine_amd64: str,
    engine_arm64: str,
    companion_amd64: str,
    companion_arm64: str,
    podman_version: str,
    machine_os: str,
    min_app_version: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "version": version,
        "engine": {"linux/amd64": engine_amd64, "linux/arm64": engine_arm64},
        "companion": {
            "safent-ads": {"linux/amd64": companion_amd64, "linux/arm64": companion_arm64},
        },
        "runtime_bundle": {"podman": podman_version, "machine_os": machine_os},
        "min_app_version": min_app_version or version,
    }


class DigestValidationError(ValueError):
    """A digest argument is not `sha256:<hex>` — refuses to sign a mutable tag."""


def _require_digest(label: str, value: str) -> None:
    if not value.startswith(_REQUIRED_DIGEST_PREFIX):
        raise DigestValidationError(
            f"{label} must start with '{_REQUIRED_DIGEST_PREFIX}' (got {value!r}) — "
            "a tag is not acceptable here (contracts/update.md invariant #1: all digest, no tag)."
        )


def validate_payload_digests(payload: dict[str, object]) -> None:
    engine = payload["engine"]
    companion_all = payload["companion"]
    assert isinstance(engine, dict)
    assert isinstance(companion_all, dict)
    companion = companion_all["safent-ads"]
    assert isinstance(companion, dict)
    for label, value in (
        ("engine linux/amd64", engine["linux/amd64"]),
        ("engine linux/arm64", engine["linux/arm64"]),
        ("companion safent-ads linux/amd64", companion["linux/amd64"]),
        ("companion safent-ads linux/arm64", companion["linux/arm64"]),
    ):
        _require_digest(label, value)


class MinisignNotAvailableError(RuntimeError):
    """The `minisign` binary is not on PATH."""


def sign_with_minisign(
    manifest_path: Path, secret_key_path: Path, *, trusted_comment: str
) -> Path:
    """Sign `manifest_path` with the real `minisign` CLI. Returns the
    `.minisig` path. Raises MinisignNotAvailableError or
    subprocess.CalledProcessError — never silently produces a bad file."""
    binary = shutil.which("minisign")
    if binary is None:
        raise MinisignNotAvailableError(
            "minisign binary not found on PATH — install it, or omit "
            "--minisign-secret-key and sign externally with the same command "
            "this would have run: minisign -S -s <key> -m <manifest>"
        )
    sig_path = manifest_path.with_name(manifest_path.name + ".minisig")
    subprocess.run(  # noqa: S603 - fixed argv, no shell, binary resolved via shutil.which
        [
            binary,
            "-S",
            "-s",
            str(secret_key_path),
            "-m",
            str(manifest_path),
            "-x",
            str(sig_path),
            "-t",
            trusted_comment,
            "-q",
        ],
        stdin=subprocess.DEVNULL,  # a password-protected key fails fast, never hangs
        check=True,
        timeout=_MINISIGN_TIMEOUT_S,
    )
    return sig_path


def _cmd_sign(args: argparse.Namespace) -> int:
    payload = build_payload(
        version=args.version,
        engine_amd64=args.engine_amd64,
        engine_arm64=args.engine_arm64,
        companion_amd64=args.companion_amd64,
        companion_arm64=args.companion_arm64,
        podman_version=args.podman_version,
        machine_os=args.machine_os,
        min_app_version=args.min_app_version,
    )
    try:
        validate_payload_digests(payload)
    except DigestValidationError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"[ok] wrote {out_path}", file=sys.stderr)

    if args.minisign_secret_key is None:
        print(
            "[*] Not signed — sign it yourself with: "
            f"minisign -S -s <key> -m {out_path}",
            file=sys.stderr,
        )
        return 0

    trusted_comment = args.minisign_trusted_comment or f"runtime-manifest v{args.version}"
    try:
        sig_path = sign_with_minisign(
            out_path, Path(args.minisign_secret_key), trusted_comment=trusted_comment
        )
    except (
        MinisignNotAvailableError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"[x] signing failed: {exc}", file=sys.stderr)
        return 1
    print(f"[ok] wrote {sig_path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sign = sub.add_parser("sign", help="assemble (+ optionally sign) runtime-manifest.json")
    sign.add_argument("--version", required=True, help="app version this manifest describes")
    sign.add_argument("--engine-amd64", required=True, help="sha256:<hex>, engine, linux/amd64")
    sign.add_argument("--engine-arm64", required=True, help="sha256:<hex>, engine, linux/arm64")
    sign.add_argument("--companion-amd64", required=True, help="sha256:<hex>, safent-ads, amd64")
    sign.add_argument("--companion-arm64", required=True, help="sha256:<hex>, safent-ads, arm64")
    sign.add_argument("--podman-version", required=True)
    sign.add_argument("--machine-os", required=True)
    sign.add_argument("--min-app-version", default=None, help="defaults to --version")
    sign.add_argument("--out", default="runtime-manifest.json")
    sign.add_argument(
        "--minisign-secret-key",
        default=None,
        help="path to an UNENCRYPTED minisign secret key (minisign -G -W); "
        "when given, this script also produces <out>.minisig",
    )
    sign.add_argument(
        "--minisign-trusted-comment", default=None, help="defaults to 'runtime-manifest v<version>'"
    )
    sign.set_defaults(func=_cmd_sign)

    parsed = parser.parse_args(argv)
    return int(parsed.func(parsed))


if __name__ == "__main__":
    raise SystemExit(main())
