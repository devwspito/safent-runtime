#!/usr/bin/env bash
# resolve-image-digest.sh — two small, composable functions for per-build
# engine/companion digest injection (SAFENT_ENGINE_DIGEST/
# SAFENT_COMPANION_DIGEST). Sourced, never executed directly — kept separate
# from stage-runtime.sh so both are testable on their own
# (tests/test-resolve-image-digest.sh).
#
# Why this exists: runtime-manifest.lock's engine_image/companion_image
# digests are a human/release-pipeline pin, `null` until ops-release-
# publisher fixes them for a specific checkout (see the lock's own note on
# engine_image). A macOS packaging run hit exactly that: null shipped
# straight through to runtime-bundle.json, and boot.rs correctly refused to
# guess (EngineDigestMissing) — the notarized DMG could not start the
# engine. The env vars let a SPECIFIC build inject the digest it actually
# published, WITHOUT requiring a hand-edit of the committed lock file for
# every release, while still treating an ALREADY-pinned lock digest as
# authoritative: an env var that disagrees with a real pin is a hard error,
# never a silent repoint (that would let a compromised or misconfigured
# pipeline point a "released" build at an arbitrary image).
set -uo pipefail

# resolve_image_digest <label> <lock_digest_or_empty> <env_value_or_empty>
# Prints the resolved digest on success (possibly empty — "still unpinned",
# the same legitimate state as today, NOT an error). Non-zero + a message on
# stderr for a malformed env value or a real mismatch against a lock pin.
resolve_image_digest() {
  local label="$1" lock_digest="$2" env_value="$3"

  if [ -z "$env_value" ]; then
    printf '%s' "$lock_digest"
    return 0
  fi

  if [[ ! "$env_value" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "[x] ${label}: '$env_value' is not a valid digest (want sha256:<64 lowercase hex>)" >&2
    return 1
  fi

  if [ -n "$lock_digest" ] && [ "$lock_digest" != "$env_value" ]; then
    echo "[x] ${label}: env var digest ($env_value) does not match runtime-manifest.lock's" \
      "pinned digest ($lock_digest) — a pinned release cannot be silently repointed" >&2
    return 1
  fi

  printf '%s' "$env_value"
  return 0
}

# platform_for_target <triple> — the Linux VM architecture the container
# ENGINE actually runs under for a given build target: on macOS this is the
# podman machine's guest (always Linux, regardless of host OS), on Linux
# it's the host itself. aarch64-apple-darwin and aarch64-unknown-linux-gnu
# both run linux/arm64 containers; x86_64-unknown-linux-gnu runs linux/amd64.
# Mirrors update::types::container_platform_key()'s OS/arch convention
# (boot.rs computes its own copy of this same mapping for its own arch, at
# runtime, to cross-check against what gets recorded here).
platform_for_target() {
  case "$1" in
    aarch64-apple-darwin|aarch64-unknown-linux-gnu) printf 'linux/arm64' ;;
    x86_64-unknown-linux-gnu) printf 'linux/amd64' ;;
    *)
      echo "[x] platform_for_target: unrecognized target: $1" >&2
      return 1
      ;;
  esac
}
