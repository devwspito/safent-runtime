#!/usr/bin/env bash
# test-resolve-image-digest.sh — proves resolve_image_digest's override-or-
# match contract (lib/resolve-image-digest.sh) and platform_for_target's
# mapping, in isolation, no staging/network involved.
#
# Exists because a real macOS packaging run stopped at engine_digest_missing:
# runtime-manifest.lock's engine_image.digest was null (not pinned for that
# checkout yet), so runtime-bundle.json shipped it null too, and boot.rs
# correctly refused to guess. SAFENT_ENGINE_DIGEST/SAFENT_COMPANION_DIGEST
# let a specific build inject the digest it actually published.
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TESTS_DIR/.." && pwd)"

# shellcheck source=../lib/resolve-image-digest.sh
source "$SCRIPTS_DIR/lib/resolve-image-digest.sh"

fail() { echo "[x] FAIL: $1" >&2; exit 1; }
pass() { echo "[ok] $1"; }

VALID_A="sha256:0000000000000000000000000000000000000000000000000000000000000001"
VALID_B="sha256:0000000000000000000000000000000000000000000000000000000000000002"

echo "[*] env unset, lock null -> stays null (ships through unchanged, not an error)"
out="$(resolve_image_digest label "" "")"
[ -z "$out" ] || fail "expected empty, got '$out'"
pass "env unset + lock null -> empty"

echo "[*] env unset, lock pinned -> the pinned value, untouched"
out="$(resolve_image_digest label "$VALID_A" "")"
[ "$out" = "$VALID_A" ] || fail "expected $VALID_A, got '$out'"
pass "env unset + lock pinned -> lock's value"

echo "[*] env set (valid), lock null -> the env value (override applied)"
out="$(resolve_image_digest label "" "$VALID_A")"
[ "$out" = "$VALID_A" ] || fail "expected $VALID_A, got '$out'"
pass "env set + lock null -> env's value (override)"

echo "[*] env set (valid), lock pinned, SAME value -> no error, that value"
out="$(resolve_image_digest label "$VALID_A" "$VALID_A")"
[ "$out" = "$VALID_A" ] || fail "expected $VALID_A, got '$out'"
pass "env set + lock pinned + match -> no conflict"

echo "[*] env set (valid), lock pinned, DIFFERENT value -> hard error, no output trusted"
if resolve_image_digest label "$VALID_A" "$VALID_B" >/tmp/riD-out.$$ 2>/tmp/riD-err.$$; then
  fail "a pinned lock digest must never be silently repointed by a mismatched env var"
fi
grep -qi "does not match" /tmp/riD-err.$$ || fail "expected a clear mismatch message on stderr"
rm -f /tmp/riD-out.$$ /tmp/riD-err.$$
pass "env set + lock pinned + mismatch -> hard error"

echo "[*] bad formats are all rejected, regardless of lock state"
for bad in "sha256:tooshort" "0000000000000000000000000000000000000000000000000000000000000001" \
           "sha256:0000000000000000000000000000000000000000000000000000000000000A" \
           "md5:0000000000000000000000000000000000000000000000000000000000000001"; do
  if resolve_image_digest label "" "$bad" >/dev/null 2>/tmp/riD-err.$$; then
    fail "'$bad' must have been rejected as malformed"
  fi
  grep -qi "not a valid digest" /tmp/riD-err.$$ || fail "'$bad': expected a 'not a valid digest' message"
  rm -f /tmp/riD-err.$$
done
pass "every malformed digest value rejected with a clear message"

echo "[*] platform_for_target maps every real target correctly"
[ "$(platform_for_target aarch64-apple-darwin)" = "linux/arm64" ] || fail "aarch64-apple-darwin should map to linux/arm64"
[ "$(platform_for_target aarch64-unknown-linux-gnu)" = "linux/arm64" ] || fail "aarch64-unknown-linux-gnu should map to linux/arm64"
[ "$(platform_for_target x86_64-unknown-linux-gnu)" = "linux/amd64" ] || fail "x86_64-unknown-linux-gnu should map to linux/amd64"
if platform_for_target bogus-target >/dev/null 2>&1; then
  fail "an unrecognized target must be rejected, not silently mapped"
fi
pass "platform_for_target: all 3 real targets correct, unrecognized target rejected"

echo "[ok] all resolve_image_digest / platform_for_target assertions passed"
