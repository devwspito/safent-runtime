#!/usr/bin/env bash
# test-runtime-bundle-image-digests.sh — MAC-03 (verificacion-mac-1.md):
# _write_runtime_bundle_manifest must carry engine_image/companion_image
# straight from runtime-manifest.lock into the staged runtime-bundle.json,
# so boot.rs/selftest.rs can read a real digest instead of requiring
# SAFENT_ENGINE_DIGEST (an env var nothing in the real pipeline ever sets).
#
# Also covers the per-build digest injection contract added on top of that
# (SAFENT_ENGINE_DIGEST/SAFENT_COMPANION_DIGEST overriding a null lock digest,
# rejecting a mismatch against an already-pinned one, and the recorded
# `platform` field) at the INTEGRATION level, i.e. through the real
# _write_runtime_bundle_manifest wiring — resolve-image-digest.sh's own
# override-or-match contract is unit-tested in isolation by
# test-resolve-image-digest.sh.
#
# Isolated: extracts ONLY that one function's real source (never sourced
# whole — the rest of stage-runtime.sh's top-level code downloads real
# podman over the network for a real target) and exercises it against
# synthetic fixtures. No network, no container (Constitution Principle V).
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TESTS_DIR/.." && pwd)"

fail() { echo "[x] $*" >&2; exit 1; }
pass() { echo "[ok] $*"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM

# SHA256() lives in lib/fetch-verified.sh; _write_runtime_bundle_manifest
# calls it directly.
# shellcheck source=../lib/fetch-verified.sh
source "$SCRIPTS_DIR/lib/fetch-verified.sh"
# resolve_image_digest()/platform_for_target() — _write_runtime_bundle_
# manifest calls both directly, same as it calls SHA256() above.
# shellcheck source=../lib/resolve-image-digest.sh
source "$SCRIPTS_DIR/lib/resolve-image-digest.sh"

# _write_runtime_bundle_manifest exits "$EXIT_USAGE" (never `return`) on a
# rejected digest injection — the real script defines this constant at its
# own top level; this harness never sources that far, so it must match it
# by hand (stage-runtime.sh: EXIT_USAGE=1). Read only inside the extracted,
# eval'd _write_runtime_bundle_manifest fragment below, so static analysis
# cannot see the read and reports a false "assigned but never read".
# shellcheck disable=SC2034
EXIT_USAGE=1

# Extract _write_runtime_bundle_manifest() alone — since the owner's
# 11-sep-2026 simplification (macOS integrity = codesign's own bundle
# seal, not a per-file cdhash this function used to also compute) it no
# longer calls any OTHER helper stage-runtime.sh defines, so there is
# nothing else to pull in first. Stops at the first column-0 `}` seen
# AFTER its own opening line, regardless of exact line numbers.
FUNC_SRC="$(awk '
  /^_write_runtime_bundle_manifest\(\) \{$/ { printing=1; in_target=1 }
  printing { print }
  in_target && /^}$/ { exit }
' "$SCRIPTS_DIR/stage-runtime.sh")"
[ -n "$FUNC_SRC" ] || fail "could not extract _write_runtime_bundle_manifest from stage-runtime.sh — did it get renamed?"
eval "$FUNC_SRC"

run_case() {
  local label="$1" lockfile_body="$2"
  local dest="$WORK/$label/dest"
  mkdir -p "$dest"
  echo "dummy podman binary" > "$dest/podman"
  echo "dummy safent script" > "$dest/safent"
  local lockfile="$WORK/$label/runtime-manifest.lock"
  printf '%s' "$lockfile_body" > "$lockfile"

  # The variables _write_runtime_bundle_manifest reads directly (no
  # parameters — matches how stage-runtime.sh's own top level calls it).
  # SAFENT_ENGINE_DIGEST/SAFENT_COMPANION_DIGEST default to empty so a case
  # that doesn't set them never inherits one leaked by an earlier case or
  # the outer environment.
  SAFENT_ENGINE_DIGEST="${3:-}" SAFENT_COMPANION_DIGEST="${4:-}" \
    TARGET="aarch64-unknown-linux-gnu" LOCKFILE="$lockfile" DEST="$dest" \
    _write_runtime_bundle_manifest >/dev/null

  echo "$dest/runtime-bundle.json"
}

# Runs _write_runtime_bundle_manifest inside a SUBSHELL and asserts it exits
# non-zero: the function itself calls `exit "$EXIT_USAGE"` (never `return`)
# on a rejected injection, which would otherwise tear down this whole test
# script rather than just "fail this one case". Prints the captured stderr
# so the caller can grep it for the expected message.
run_case_expect_failure() {
  local label="$1" lockfile_body="$2" engine_env="${3:-}" companion_env="${4:-}"
  local dest="$WORK/$label/dest"
  mkdir -p "$dest"
  echo "dummy podman binary" > "$dest/podman"
  echo "dummy safent script" > "$dest/safent"
  local lockfile="$WORK/$label/runtime-manifest.lock"
  printf '%s' "$lockfile_body" > "$lockfile"

  local errfile="$WORK/$label.err" rc=0
  ( SAFENT_ENGINE_DIGEST="$engine_env" SAFENT_COMPANION_DIGEST="$companion_env" \
    TARGET="aarch64-unknown-linux-gnu" LOCKFILE="$lockfile" DEST="$dest" \
    _write_runtime_bundle_manifest >/dev/null 2>"$errfile" ) || rc=$?
  [ "$rc" -ne 0 ] || fail "$label: expected _write_runtime_bundle_manifest to reject this input, it exited 0"
  cat "$errfile"
}

# Case 1: both engine and companion digests pinned.
out="$(run_case "both-pinned" '{
  "targets": {"aarch64-unknown-linux-gnu": {"podman_version": "6.1.1"}},
  "engine_image": {"repo": "ghcr.io/devwspito/safent", "digest": "sha256:engine-good"},
  "companion_image": {"repo": "ghcr.io/devwspito/safent-ads", "digest": "sha256:ads-good"}
}')"
[ "$(jq -r '.engine_image.repo' "$out")" = "ghcr.io/devwspito/safent" ] || fail "engine_image.repo not propagated (both-pinned)"
[ "$(jq -r '.engine_image.digest' "$out")" = "sha256:engine-good" ] || fail "engine_image.digest not propagated (both-pinned)"
[ "$(jq -r '.companion_image.repo' "$out")" = "ghcr.io/devwspito/safent-ads" ] || fail "companion_image.repo not propagated (both-pinned)"
[ "$(jq -r '.companion_image.digest' "$out")" = "sha256:ads-good" ] || fail "companion_image.digest not propagated (both-pinned)"
[ "$(jq -r '.engine_image.platform' "$out")" = "linux/arm64" ] || fail "engine_image.platform must record platform_for_target(TARGET) (both-pinned)"
[ "$(jq -r '.companion_image.platform' "$out")" = "linux/arm64" ] || fail "companion_image.platform must record platform_for_target(TARGET) (both-pinned)"
pass "both engine_image and companion_image propagate repo+digest+platform verbatim"

# Case 2: engine pinned, digest not yet fixed (null) — a legitimate
# "release pipeline has not run yet" state, must ship through as null, not
# be dropped or turned into the string "null".
out="$(run_case "null-digest" '{
  "targets": {"aarch64-unknown-linux-gnu": {"podman_version": "6.1.1"}},
  "engine_image": {"repo": "ghcr.io/devwspito/safent", "digest": null}
}')"
[ "$(jq -r '.engine_image.repo' "$out")" = "ghcr.io/devwspito/safent" ] || fail "engine_image.repo not propagated (null-digest)"
[ "$(jq '.engine_image.digest' "$out")" = "null" ] || fail "engine_image.digest must ship as JSON null, not a string or absent (null-digest)"
[ "$(jq '.companion_image' "$out")" = "null" ] || fail "companion_image must be null when absent from the lock (null-digest)"
pass "a null digest ships through as JSON null, not dropped or stringified"

# Case 3: neither field present in the lock at all (an OLDER lock, or one
# never touched by this pass) — must still produce a valid manifest with
# explicit nulls, never a missing key or a jq error.
out="$(run_case "no-image-fields" '{
  "targets": {"aarch64-unknown-linux-gnu": {"podman_version": "6.1.1"}}
}')"
[ "$(jq '.engine_image' "$out")" = "null" ] || fail "engine_image must be null when the lock has no such key at all (no-image-fields)"
[ "$(jq '.companion_image' "$out")" = "null" ] || fail "companion_image must be null when the lock has no such key at all (no-image-fields)"
[ "$(jq '.entries | length' "$out")" = "2" ] || fail "the two dummy staged files must still be recorded in entries (no-image-fields)"
pass "an older lock with neither field still produces a valid manifest (explicit nulls, no crash)"

# Case 4: SAFENT_ENGINE_DIGEST/SAFENT_COMPANION_DIGEST inject a digest for a
# checkout the lock has NOT pinned yet (null) — the exact scenario that
# stopped a real macOS packaging run at engine_digest_missing.
out="$(run_case "env-override-null-lock" '{
  "targets": {"aarch64-unknown-linux-gnu": {"podman_version": "6.1.1"}},
  "engine_image": {"repo": "ghcr.io/devwspito/safent", "digest": null},
  "companion_image": {"repo": "ghcr.io/devwspito/safent-ads", "digest": null}
}' "sha256:$(printf 'e%.0s' {1..64})" "sha256:$(printf 'c%.0s' {1..64})")"
[ "$(jq -r '.engine_image.digest' "$out")" = "sha256:$(printf 'e%.0s' {1..64})" ] || fail "SAFENT_ENGINE_DIGEST must override a null lock digest"
[ "$(jq -r '.companion_image.digest' "$out")" = "sha256:$(printf 'c%.0s' {1..64})" ] || fail "SAFENT_COMPANION_DIGEST must override a null lock digest"
[ "$(jq -r '.engine_image.platform' "$out")" = "linux/arm64" ] || fail "an injected engine digest must still record platform (env-override-null-lock)"
pass "SAFENT_ENGINE_DIGEST/SAFENT_COMPANION_DIGEST override a null lock digest and still record platform"

# Case 5: a lock that ALREADY pins a digest can never be silently repointed
# by a disagreeing env var — hard error, and runtime-bundle.json must not
# even be written (fail before any output, not a half-written file).
GOOD="sha256:$(printf 'b%.0s' {1..64})"
err="$(run_case_expect_failure "env-mismatch" '{
  "targets": {"aarch64-unknown-linux-gnu": {"podman_version": "6.1.1"}},
  "engine_image": {"repo": "ghcr.io/devwspito/safent", "digest": "'"$GOOD"'"}
}' "sha256:$(printf 'f%.0s' {1..64})")"
echo "$err" | grep -qi "does not match" || fail "env-mismatch: expected a 'does not match' message, got: $err"
[ ! -e "$WORK/env-mismatch/dest/runtime-bundle.json" ] || fail "env-mismatch: runtime-bundle.json must not be written on a rejected injection"
pass "a mismatched SAFENT_ENGINE_DIGEST against an already-pinned lock is a hard error, no output written"

# Case 6: a malformed digest value is rejected the same way, wired all the
# way through the real function (resolve-image-digest.sh's own format
# validation is unit-tested directly by test-resolve-image-digest.sh).
err="$(run_case_expect_failure "env-bad-format" '{
  "targets": {"aarch64-unknown-linux-gnu": {"podman_version": "6.1.1"}},
  "engine_image": {"repo": "ghcr.io/devwspito/safent", "digest": null}
}' "not-a-real-digest")"
echo "$err" | grep -qi "not a valid digest" || fail "env-bad-format: expected a 'not a valid digest' message, got: $err"
pass "a malformed SAFENT_ENGINE_DIGEST is rejected through the real _write_runtime_bundle_manifest wiring"

echo "[ok] test-runtime-bundle-image-digests.sh: all cases passed"
