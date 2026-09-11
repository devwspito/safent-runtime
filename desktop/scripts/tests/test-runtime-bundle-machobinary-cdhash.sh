#!/usr/bin/env bash
# test-runtime-bundle-machobinary-cdhash.sh — MAC-02 (verificacion-mac-1.md):
# _write_runtime_bundle_manifest must classify Mach-O files (portable magic-
# byte sniff) and record a REAL cdhash (via codesign, faked here) for them,
# leaving sha256 as the ONLY check for everything else — codesigning
# rewrites a Mach-O's bytes, permanently invalidating its pre-sign sha256.
# Isolated: extracts the CODESIGN/_is_macho/_write_runtime_bundle_manifest
# fragment (never sources the whole network-touching stage-runtime.sh) and
# exercises it with a fake `codesign`. No network, no container
# (Constitution Principle V).
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TESTS_DIR/.." && pwd)"

fail() { echo "[x] $*" >&2; exit 1; }
pass() { echo "[ok] $*"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM

# shellcheck source=../lib/fetch-verified.sh
source "$SCRIPTS_DIR/lib/fetch-verified.sh"
# resolve_image_digest()/platform_for_target() — _write_runtime_bundle_
# manifest calls both directly, same as it calls SHA256() above.
# shellcheck source=../lib/resolve-image-digest.sh
source "$SCRIPTS_DIR/lib/resolve-image-digest.sh"

# _write_runtime_bundle_manifest exits "$EXIT_USAGE" (never `return`) on a
# rejected digest injection — matches stage-runtime.sh's own top-level
# constant (EXIT_USAGE=1), read only inside the extracted, eval'd fragment
# below, so static analysis reports a false "assigned but never read".
# shellcheck disable=SC2034
EXIT_USAGE=1

# Extracts CODESIGN=..., _is_macho(), and _write_runtime_bundle_manifest()
# as ONE contiguous fragment, regardless of exact line numbers: starts at
# the CODESIGN= assignment, stops at the first column-0 `}` seen AFTER
# _write_runtime_bundle_manifest's own opening line.
FUNC_SRC="$(awk '
  /^CODESIGN=/ { printing=1 }
  printing { print }
  /^_write_runtime_bundle_manifest\(\) \{$/ { in_target=1 }
  in_target && /^}$/ { exit }
' "$SCRIPTS_DIR/stage-runtime.sh")"
[ -n "$FUNC_SRC" ] || fail "could not extract the CODESIGN/_is_macho/_write_runtime_bundle_manifest fragment — did stage-runtime.sh change shape?"

# Fake codesign: `--verify --strict <path>` succeeds iff the path is
# registered as "signed" (its basename has an entry under
# FAKE_CDHASHES_DIR); `-dvvv <path>` prints a CDHash= line with whatever
# was registered.
FAKE_CDHASHES_DIR="$WORK/cdhashes"
mkdir -p "$FAKE_CDHASHES_DIR"
FAKE_CODESIGN="$WORK/bin/codesign"
mkdir -p "$WORK/bin"
cat > "$FAKE_CODESIGN" <<'EOF'
#!/bin/sh
case "$1" in
  --verify)
    shift
    [ "$1" = "--strict" ] && shift
    path="$1"
    [ -f "$FAKE_CDHASHES_DIR/$(basename "$path")" ] || exit 1
    exit 0
    ;;
  -dvvv)
    path="$2"
    f="$FAKE_CDHASHES_DIR/$(basename "$path")"
    [ -f "$f" ] || exit 1
    # real `codesign -d` reports on STDERR; the fake must too, or the test
    # passes against behaviour the real tool never has (it did once).
    echo "Executable=$path" >&2
    echo "CDHash=$(cat "$f")" >&2
    exit 0
    ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$FAKE_CODESIGN"
export FAKE_CDHASHES_DIR
export SAFENT_CODESIGN="$FAKE_CODESIGN"
eval "$FUNC_SRC"

# Case 1: a signed Mach-O (fake codesign has a registered cdhash for it)
# alongside a non-Mach-O file.
dest="$WORK/signed/dest"
mkdir -p "$dest"
# A genuine Mach-O (64-bit) magic prefix — enough for the portable
# magic-byte classifier, not a real executable.
printf '\xcf\xfa\xed\xfe' > "$dest/podman"
echo "dummy safent script, not a Mach-O" > "$dest/safent"
echo "cafef00dcafef00dcafef00dcafef00dcafef00d" > "$FAKE_CDHASHES_DIR/podman"
lockfile="$WORK/signed/runtime-manifest.lock"
printf '%s' '{"targets": {"aarch64-apple-darwin": {"podman_version": "6.1.1"}}}' > "$lockfile"
TARGET="aarch64-apple-darwin" LOCKFILE="$lockfile" DEST="$dest" _write_runtime_bundle_manifest >/dev/null

out="$dest/runtime-bundle.json"
podman_cdhash="$(jq -r '.entries[] | select(.path=="podman") | .cdhash' "$out")"
[ "$podman_cdhash" = "cafef00dcafef00dcafef00dcafef00dcafef00d" ] || fail "Mach-O cdhash not recorded correctly: got $podman_cdhash"
safent_cdhash="$(jq '.entries[] | select(.path=="safent") | .cdhash' "$out")"
[ "$safent_cdhash" = "null" ] || fail "non-Mach-O file must have cdhash: null, got $safent_cdhash"
safent_sha="$(jq -r '.entries[] | select(.path=="safent") | .sha256' "$out")"
[ "$safent_sha" = "$(SHA256 "$dest/safent")" ] || fail "non-Mach-O sha256 must still be recorded"
pass "a Mach-O file gets a real cdhash; a non-Mach-O file gets cdhash:null + sha256"

# Case 2: a Mach-O file with NO registered cdhash (fake codesign reports
# "not signed", CDHash empty) — must gracefully record cdhash:null, not
# crash and not invent a value. A FRESH FAKE_CDHASHES_DIR (case 1 already
# registered a "podman" entry there).
rm -rf "$FAKE_CDHASHES_DIR"
mkdir -p "$FAKE_CDHASHES_DIR"
dest2="$WORK/unsigned/dest"
mkdir -p "$dest2"
printf '\xcf\xfa\xed\xfe' > "$dest2/podman"
lockfile2="$WORK/unsigned/runtime-manifest.lock"
printf '%s' '{"targets": {"aarch64-apple-darwin": {"podman_version": "6.1.1"}}}' > "$lockfile2"
TARGET="aarch64-apple-darwin" LOCKFILE="$lockfile2" DEST="$dest2" _write_runtime_bundle_manifest >/dev/null
out2="$dest2/runtime-bundle.json"
podman_cdhash2="$(jq '.entries[] | select(.path=="podman") | .cdhash' "$out2")"
[ "$podman_cdhash2" = "null" ] || fail "an unsigned Mach-O must record cdhash:null, got $podman_cdhash2"
pass "an unsigned Mach-O (local dev build) records cdhash:null instead of crashing or guessing"

# Case 3: the real `--refresh-bundle-json <target>` CLI entry point end to
# end (real stage-runtime.sh, not the extracted fragment) — proves the
# pipeline's actual invocation shape works, re-scanning an ALREADY-staged
# $DEST with NO download step reachable at all. Mirrors stage-runtime.sh
# (+lib/) into a throwaway fake repo root so $SCRIPT_DIR/$DESKTOP_DIR/
# $LOCKFILE/$RESOURCES_ROOT — all computed from the SCRIPT's own on-disk
# location, never overridable via env — point at fixtures instead of the
# real repo. _stage_app_files/_record_app_files_in_lock never run in this
# mode, so none of APP_FILES needs to exist here.
fake_repo="$WORK/fake-repo"
mkdir -p "$fake_repo/desktop/scripts"
cp -R "$SCRIPTS_DIR/lib" "$fake_repo/desktop/scripts/lib"
cp "$SCRIPTS_DIR/stage-runtime.sh" "$fake_repo/desktop/scripts/stage-runtime.sh"
fake_dest="$fake_repo/desktop/src-tauri/resources/runtime/aarch64-apple-darwin"
mkdir -p "$fake_dest"
printf '\xcf\xfa\xed\xfe' > "$fake_dest/podman"
echo "already staged, pre-existing sha256 is stale on purpose" > "$fake_dest/safent"
# The manifest a PRE-signing normal staging run would have produced —
# cdhash:null for the Mach-O (not signed yet at THAT time).
cat > "$fake_dest/runtime-bundle.json" <<JSON
{"podman_version":"6.1.1","entries":[
  {"path":"podman","sha256":"stale-pre-sign-hash","cdhash":null,"mode":"0755"},
  {"path":"safent","sha256":"stale","cdhash":null,"mode":"0755"}
]}
JSON
printf '%s' '{"targets": {"aarch64-apple-darwin": {"podman_version": "6.1.1"}}}' > "$fake_repo/desktop/runtime-manifest.lock"
rm -rf "$FAKE_CDHASHES_DIR"
mkdir -p "$FAKE_CDHASHES_DIR"
echo "postsignrealcdhashvalue0123456789abcdef01234567" > "$FAKE_CDHASHES_DIR/podman"

SAFENT_CODESIGN="$FAKE_CODESIGN" timeout 10 bash "$fake_repo/desktop/scripts/stage-runtime.sh" \
  --refresh-bundle-json aarch64-apple-darwin >"$WORK/refresh.out" 2>&1
rc=$?
[ "$rc" -eq 0 ] || fail "--refresh-bundle-json exited $rc: $(cat "$WORK/refresh.out")"
refreshed="$fake_dest/runtime-bundle.json"
refreshed_cdhash="$(jq -r '.entries[] | select(.path=="podman") | .cdhash' "$refreshed")"
[ "$refreshed_cdhash" = "postsignrealcdhashvalue0123456789abcdef01234567" ] || \
  fail "--refresh-bundle-json did not record the post-sign cdhash: got $refreshed_cdhash"
refreshed_safent_sha="$(jq -r '.entries[] | select(.path=="safent") | .sha256' "$refreshed")"
[ "$refreshed_safent_sha" = "$(SHA256 "$fake_dest/safent")" ] || \
  fail "--refresh-bundle-json must recompute sha256 for non-Mach-O files too"
pass "--refresh-bundle-json <target> rewrites the ALREADY-staged manifest in place with a real post-sign cdhash, no download"

# Case 4: --refresh-bundle-json must PRESERVE a digest injected on the
# ORIGINAL staging run even when this later invocation (its own separate
# process, e.g. a signing step further down the pipeline) re-exports
# NEITHER SAFENT_ENGINE_DIGEST NOR SAFENT_COMPANION_DIGEST and the lock
# itself still has not pinned one — $out's own prior recording is the only
# remaining copy of what was injected the first time.
ENGINE_DIGEST_ORIGINAL="sha256:$(printf 'e%.0s' {1..64})"
ENGINE_DIGEST_RESUPPLIED="sha256:$(printf 'd%.0s' {1..64})"

fake_repo2="$WORK/fake-repo-preserve"
mkdir -p "$fake_repo2/desktop/scripts"
cp -R "$SCRIPTS_DIR/lib" "$fake_repo2/desktop/scripts/lib"
cp "$SCRIPTS_DIR/stage-runtime.sh" "$fake_repo2/desktop/scripts/stage-runtime.sh"
fake_dest2="$fake_repo2/desktop/src-tauri/resources/runtime/aarch64-apple-darwin"
mkdir -p "$fake_dest2"
printf '\xcf\xfa\xed\xfe' > "$fake_dest2/podman"
echo "already staged" > "$fake_dest2/safent"
cat > "$fake_dest2/runtime-bundle.json" <<JSON
{"podman_version":"6.1.1","entries":[
  {"path":"podman","sha256":"stale","cdhash":null,"mode":"0755"},
  {"path":"safent","sha256":"stale","cdhash":null,"mode":"0755"}
],
"engine_image":{"repo":"ghcr.io/devwspito/safent","digest":"$ENGINE_DIGEST_ORIGINAL","platform":"linux/arm64"}}
JSON
printf '%s' '{"targets": {"aarch64-apple-darwin": {"podman_version": "6.1.1"}},
  "engine_image": {"repo": "ghcr.io/devwspito/safent", "digest": null}}' \
  > "$fake_repo2/desktop/runtime-manifest.lock"
rm -rf "$FAKE_CDHASHES_DIR"
mkdir -p "$FAKE_CDHASHES_DIR"
echo "anothercdhash0123456789abcdef0123456789abcdef01" > "$FAKE_CDHASHES_DIR/podman"

# 4a: refresh with NEITHER env var set — the original injected digest must
# survive, not regress to null just because this process didn't repeat it.
env -u SAFENT_ENGINE_DIGEST -u SAFENT_COMPANION_DIGEST \
  timeout 10 bash "$fake_repo2/desktop/scripts/stage-runtime.sh" \
  --refresh-bundle-json aarch64-apple-darwin >"$WORK/refresh-preserve.out" 2>&1
rc=$?
[ "$rc" -eq 0 ] || fail "--refresh-bundle-json (preserve case) exited $rc: $(cat "$WORK/refresh-preserve.out")"
preserved="$(jq -r '.engine_image.digest' "$fake_dest2/runtime-bundle.json")"
[ "$preserved" = "$ENGINE_DIGEST_ORIGINAL" ] || \
  fail "--refresh-bundle-json must preserve the previously-injected engine digest, got: $preserved"
preserved_platform="$(jq -r '.engine_image.platform' "$fake_dest2/runtime-bundle.json")"
[ "$preserved_platform" = "linux/arm64" ] || \
  fail "--refresh-bundle-json must preserve/recompute the platform alongside the preserved digest, got: $preserved_platform"
pass "--refresh-bundle-json preserves a previously-injected engine digest when re-run without the env var"

# 4b: refresh WITH a freshly re-supplied (different) env var — the fresh,
# explicit input for THIS invocation must win over the merely-remembered
# prior value, never be blocked by it as though it were a lock mismatch.
SAFENT_ENGINE_DIGEST="$ENGINE_DIGEST_RESUPPLIED" \
  timeout 10 bash "$fake_repo2/desktop/scripts/stage-runtime.sh" \
  --refresh-bundle-json aarch64-apple-darwin >"$WORK/refresh-resupply.out" 2>&1
rc=$?
[ "$rc" -eq 0 ] || fail "--refresh-bundle-json (resupply case) exited $rc: $(cat "$WORK/refresh-resupply.out")"
resupplied="$(jq -r '.engine_image.digest' "$fake_dest2/runtime-bundle.json")"
[ "$resupplied" = "$ENGINE_DIGEST_RESUPPLIED" ] || \
  fail "a freshly re-supplied SAFENT_ENGINE_DIGEST on refresh must win over the prior recorded value, got: $resupplied"
pass "--refresh-bundle-json honors a freshly re-supplied SAFENT_ENGINE_DIGEST over the prior recorded value"

# Case 5: MAC2-08 (verificacion-mac-2.md) — a real signed DMG shipped with
# all 15 cdhash null despite matching sha256: codesigning runs against the
# BUILT .app's OWN copy of these files, never against
# resources/runtime/<triple>/ (nothing re-touches that after normal
# staging). The optional 3rd arg must let the pipeline point this at the
# ACTUAL signed location instead of always defaulting to the staging dir.
fake_repo3="$WORK/fake-repo-explicit-dir"
mkdir -p "$fake_repo3/desktop/scripts"
cp -R "$SCRIPTS_DIR/lib" "$fake_repo3/desktop/scripts/lib"
cp "$SCRIPTS_DIR/stage-runtime.sh" "$fake_repo3/desktop/scripts/stage-runtime.sh"
printf '%s' '{"targets": {"aarch64-apple-darwin": {"podman_version": "6.1.1"}}}' \
  > "$fake_repo3/desktop/runtime-manifest.lock"

# The default staging location — deliberately left with its pre-sign
# (null-cdhash) manifest, to prove it is NEVER touched by this case.
staging_dest="$fake_repo3/desktop/src-tauri/resources/runtime/aarch64-apple-darwin"
mkdir -p "$staging_dest"
printf '\xcf\xfa\xed\xfe' > "$staging_dest/podman"
cat > "$staging_dest/runtime-bundle.json" <<JSON
{"podman_version":"6.1.1","entries":[{"path":"podman","sha256":"staging-stale","cdhash":null,"mode":"0755"}]}
JSON

# The ACTUAL signed .app's own resource copy — a SEPARATE directory,
# standing in for Contents/Resources/runtime/ after a real build+sign.
app_dest="$WORK/Safent.app/Contents/Resources/runtime"
mkdir -p "$app_dest"
printf '\xcf\xfa\xed\xfe' > "$app_dest/podman"
cat > "$app_dest/runtime-bundle.json" <<JSON
{"podman_version":"6.1.1","entries":[{"path":"podman","sha256":"app-stale","cdhash":null,"mode":"0755"}]}
JSON
rm -rf "$FAKE_CDHASHES_DIR"
mkdir -p "$FAKE_CDHASHES_DIR"
echo "signedappcdhash0123456789abcdef0123456789abcdef" > "$FAKE_CDHASHES_DIR/podman"

SAFENT_CODESIGN="$FAKE_CODESIGN" timeout 10 bash "$fake_repo3/desktop/scripts/stage-runtime.sh" \
  --refresh-bundle-json aarch64-apple-darwin "$app_dest" >"$WORK/refresh-explicit-dir.out" 2>&1
rc=$?
[ "$rc" -eq 0 ] || fail "--refresh-bundle-json <target> <dir> exited $rc: $(cat "$WORK/refresh-explicit-dir.out")"

app_cdhash="$(jq -r '.entries[] | select(.path=="podman") | .cdhash' "$app_dest/runtime-bundle.json")"
[ "$app_cdhash" = "signedappcdhash0123456789abcdef0123456789abcdef" ] || \
  fail "the EXPLICIT dir's manifest must be the one refreshed with the real cdhash, got: $app_cdhash"
staging_cdhash="$(jq -r '.entries[] | select(.path=="podman") | .cdhash' "$staging_dest/runtime-bundle.json")"
[ "$staging_cdhash" = "null" ] || \
  fail "the DEFAULT staging dir must be left untouched when an explicit dir is given, got: $staging_cdhash"
pass "--refresh-bundle-json <target> <dir> operates on the explicit dir, never the default staging tree"

echo "[ok] test-runtime-bundle-machobinary-cdhash.sh: all cases passed"
