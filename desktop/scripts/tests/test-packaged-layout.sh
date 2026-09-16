#!/usr/bin/env bash
# test-packaged-layout.sh — packaged-layout contract test, covering TWO
# independent regressions found against the same staged tree by two lanes:
#
# 1. (packaging review item 1, specs/028-safent-app-nativa/
#    verificacion-paquete-linux.md "Comprobación 2") a signed package shipped
#    with NO safent CLI anywhere inside it — boot.rs::resolve_config had
#    nothing to invoke. Asserts the safent CLI + host launcher + companion
#    provisioning assets are present, hash-matched in BOTH
#    runtime-bundle.json and runtime-manifest.lock's .app_files, and
#    executable where required.
# 2. (a real macOS notarytool rejection, validation #12) every binary inside
#    a raw download archive (krunkit-podman-unsigned-1.3.2.tgz) staged
#    UNDER resources/runtime/ got bundled and inspected unsigned alongside
#    its own extracted/signed copy. Asserts resources/runtime/ contains
#    ONLY extracted files — no archive/.part/dotdir anywhere — and that
#    stage-runtime.sh's download cache lives outside it entirely.
#
# Runs the REAL stage-runtime.sh, from a clean slate (so regression 2's
# "nothing leaked" claim means something — a stale archive from a PREVIOUS
# run must not be able to hide behind an early idempotent skip), for the
# current host's own Linux triple by default (network-touching — same as a
# real CI matrix leg) or an explicit $1 override; skips outright on a
# non-Linux host or an unsupported arch instead of guessing.
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TESTS_DIR/.." && pwd)"
DESKTOP_DIR="$(cd "$SCRIPTS_DIR/.." && pwd)"
LOCKFILE="$DESKTOP_DIR/runtime-manifest.lock"
RESOURCES_ROOT="$DESKTOP_DIR/src-tauri/resources/runtime"

fail() { echo "[x] $*" >&2; exit 1; }
pass() { echo "[ok] $*"; }

if [ -n "${1:-}" ]; then
  TARGET="$1"
else
  case "$(uname -s)" in
    Linux) ;;
    *) echo "[skip] test-packaged-layout.sh only covers Linux triples on this host ($(uname -s)) unless given an explicit target"; exit 0 ;;
  esac
  case "$(uname -m)" in
    aarch64) TARGET=aarch64-unknown-linux-gnu ;;
    x86_64)  TARGET=x86_64-unknown-linux-gnu ;;
    *) echo "[skip] unsupported arch for this test: $(uname -m)"; exit 0 ;;
  esac
fi

DEST="$RESOURCES_ROOT/$TARGET"
MANIFEST="$DEST/runtime-bundle.json"
CACHE_DIR="$DESKTOP_DIR/.cache/runtime-downloads/$TARGET"

echo "[*] cleaning $DEST and $CACHE_DIR for a genuinely fresh run" >&2
rm -rf "$DEST" "$CACHE_DIR"

"$SCRIPTS_DIR/stage-runtime.sh" "$TARGET" >&2

[ -f "$MANIFEST" ] || fail "no runtime-bundle.json under $DEST after staging"

# ---- regression 1: the safent CLI + neighbors must be in the package -----
EXPECTED_APP_FILES="safent run-safent.sh safent.json provision.sh compose.yaml caps.template.yaml"
for name in $EXPECTED_APP_FILES; do
  [ -f "$DEST/$name" ] || fail "expected app file missing from staged tree: $name"

  bundle_sha="$(jq -r --arg p "$name" '.entries[] | select(.path == $p) | .sha256' "$MANIFEST")"
  [ -n "$bundle_sha" ] || fail "runtime-bundle.json has no entry for $name"
  got_sha="$(sha256sum "$DEST/$name" | awk '{print $1}')"
  [ "$got_sha" = "$bundle_sha" ] || fail "$name: staged file does not match its own runtime-bundle.json entry"

  lock_sha="$(jq -r --arg n "$name" '.app_files.entries[] | select(.flat_name == $n) | .sha256' "$LOCKFILE")"
  [ -n "$lock_sha" ] || fail "runtime-manifest.lock .app_files has no entry for $name"
  [ "$lock_sha" = "$got_sha" ] || fail "$name: staged sha256 disagrees with runtime-manifest.lock's .app_files record"
done
pass "all 6 app files present, hash-matched in both records: $EXPECTED_APP_FILES"

for name in safent run-safent.sh provision.sh; do
  mode="$(jq -r --arg p "$name" '.entries[] | select(.path == $p) | .mode' "$MANIFEST")"
  [ "$mode" = "0755" ] || fail "$name: runtime-bundle.json records mode $mode, want 0755"
  [ -x "$DEST/$name" ] || fail "$name: staged file is not actually executable on disk"
done
pass "safent/run-safent.sh/provision.sh are 0755 in both the manifest and on disk"

# ---- the pinned podman toolchain is still there too (not at the expense
#      of the new files) — containers.conf is special-cased: it is
#      deliberately patched post-verification (stage-runtime.sh's
#      _patch_containers_conf: helper_binaries_dir + default_rootless_
#      network_cmd + lock_type), so its lock entry pins the PRISTINE
#      upstream download on purpose and the staged file never matches that
#      again — checked against containers_conf_patch instead, same special
#      case as _already_staged() itself. ----------------------------------
n="$(jq -r --arg t "$TARGET" '.targets[$t].entries | length' "$LOCKFILE")"
i=0
while [ "$i" -lt "$n" ]; do
  path="$(jq -r --arg t "$TARGET" ".targets[\$t].entries[$i].path" "$LOCKFILE")"
  want_sha="$(jq -r --arg t "$TARGET" ".targets[\$t].entries[$i].sha256" "$LOCKFILE")"
  base="$(basename "$path")"
  [ -f "$DEST/$path" ] || fail "podman toolchain file missing from staged tree: $path"
  if [ "$path" = "etc/containers/containers.conf" ]; then
    patched_sha="$(jq -r --arg t "$TARGET" '.targets[$t].containers_conf_patch.sha256' "$LOCKFILE")"
    got_sha="$(sha256sum "$DEST/$path" | awk '{print $1}')"
    [ "$got_sha" = "$patched_sha" ] || fail "$path: staged sha256 disagrees with runtime-manifest.lock's containers_conf_patch"
    grep -q '^lock_type = "file"' "$DEST/$path" || fail "$path: not patched with lock_type"
    grep -q '^helper_binaries_dir' "$DEST/$path" || fail "$path: not patched with helper_binaries_dir"
    i=$((i + 1))
    continue
  fi
  got_sha="$(sha256sum "$DEST/$path" | awk '{print $1}')"
  [ "$got_sha" = "$want_sha" ] || fail "$path: staged sha256 disagrees with runtime-manifest.lock"
  bundle_sha="$(jq -r --arg p "$base" '.entries[] | select(.path == $p) | .sha256' "$MANIFEST")"
  [ "$bundle_sha" = "$want_sha" ] || fail "$base: runtime-bundle.json's flattened record disagrees with runtime-manifest.lock"
  i=$((i + 1))
done
pass "all $n pinned podman toolchain entries from runtime-manifest.lock present (nested on disk, flattened in runtime-bundle.json)"

total="$(jq '.entries | length' "$MANIFEST")"
# MAC3-03 (verificacion-mac-3.md): +6, not +5 — ops/container/seccomp/
# safent.json joined APP_FILES (stage-runtime.sh) as a 6th bundled,
# hash-verified app file, so _ensure_seccomp can resolve it from the
# packaged resources instead of fetching it into $SAFENT_STATE_HOME at
# runtime (a host path that must be VM-visible on macOS).
want_total=$((n + 1 + 6)) # +1 for bin/pasta (staged as a real copy, not counted in .targets[].entries)
[ "$total" -eq "$want_total" ] || fail "runtime-bundle.json has $total entries, want $want_total ($n podman + 1 pasta + 6 app files)"
pass "runtime-bundle.json entry count matches exactly: $total"

# ---- regression 2: no raw archives/partials/dotdirs under resources/ -----
FORBIDDEN_PATTERNS=('*.tgz' '*.tar.gz' '*.zip' '*.pkg' '*.part' '*.verified')
found=0
for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
  while IFS= read -r -d '' hit; do
    echo "    forbidden: $hit (matches $pattern)" >&2
    found=1
  done < <(find "$RESOURCES_ROOT" -iname "$pattern" -print0)
done
[ "$found" -eq 0 ] || fail "found archive/partial/marker file(s) under $RESOURCES_ROOT — these get bundled and break notarization"
pass "no archive extensions anywhere under resources/runtime/"

while IFS= read -r -d '' d; do
  fail "dotdir under resources/runtime/: $d — bundle.resources' glob matches dot-entries too, this gets bundled"
done < <(find "$RESOURCES_ROOT" -type d -name '.*' -print0)
pass "no .cache (or any dotdir) anywhere under resources/runtime/"

[ -d "$CACHE_DIR" ] || fail "expected a cache dir at $CACHE_DIR — did CACHE_DIR move back under resources/runtime/?"
cache_file_count="$(find "$CACHE_DIR" -type f | wc -l | tr -d ' ')"
[ "$cache_file_count" -gt 0 ] || fail "expected at least one cached archive under $CACHE_DIR"
case "$CACHE_DIR" in
  "$RESOURCES_ROOT"*) fail "CACHE_DIR ($CACHE_DIR) is under resources/runtime/ ($RESOURCES_ROOT) — this is exactly the bug" ;;
esac
pass "cache dir ($cache_file_count file(s)) confirmed outside resources/runtime/: $CACHE_DIR"

echo "[ok] test-packaged-layout.sh: packaged layout for $TARGET is contract-complete (app files + clean resources tree)"
