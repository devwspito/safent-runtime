#!/usr/bin/env bash
# test-normalize-staged-tree.sh — proves normalize_staged_tree
# (lib/normalize-staged-tree.sh) fixes exactly what broke the real macOS
# pipeline run: files copied read-only from an upstream release tarball
# (krunkit v1.3.2 ships bin/krunkit 0555 and three of its four .dylib files
# 0444 — reproduced here with the SAME bits, not a stand-in), plus a symlink,
# normalized into a tree where every entry is owner-writable, has the right
# mode, and contains no symlinks — the precondition tauri-bundler's
# `xattr -crs <bundle>` (crates/tauri-bundler/src/bundle/macos/app.rs,
# verified against the real 2.9.4 source) needs to not fail.
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TESTS_DIR/.." && pwd)"

# shellcheck source=../lib/normalize-staged-tree.sh
source "$SCRIPTS_DIR/lib/normalize-staged-tree.sh"

WORK="$(mktemp -d)"
trap 'chmod -R u+w "$WORK" 2>/dev/null || true; rm -rf "$WORK"' EXIT INT TERM

fail() {
  echo "[x] FAIL: $1" >&2
  exit 1
}

DEST="$WORK/aarch64-apple-darwin"
mkdir -p "$DEST/bin" "$DEST/lib" "$DEST/share/krunkit" "$DEST/machine"

# Reproduce the EXACT confirmed-bad permissions from the real krunkit v1.3.2
# release tarball (see this fix's commit message for the `tar tzv` evidence).
printf 'fake krunkit elf\n' >"$DEST/bin/krunkit"
chmod 0555 "$DEST/bin/krunkit"                      # no owner-write — as shipped
printf 'fake libkrun\n' >"$DEST/lib/libkrun.dylib"
chmod 0444 "$DEST/lib/libkrun.dylib"                 # no write bit at all — as shipped
printf 'fake moltenvk\n' >"$DEST/lib/libMoltenVK.dylib"
chmod 0444 "$DEST/lib/libMoltenVK.dylib"             # as shipped
printf 'fake virgl\n' >"$DEST/lib/libvirglrenderer.1.dylib"
chmod 0444 "$DEST/lib/libvirglrenderer.1.dylib"      # as shipped
printf 'fake epoxy\n' >"$DEST/lib/libepoxy.0.dylib"
chmod 0755 "$DEST/lib/libepoxy.0.dylib"              # this one ships fine already
printf 'firmware blob\n' >"$DEST/share/krunkit/KRUN_EFI.silent.fd"
chmod 0644 "$DEST/share/krunkit/KRUN_EFI.silent.fd"  # this one ships fine already
printf 'fake podman\n' >"$DEST/bin/podman"
chmod 0755 "$DEST/bin/podman"
printf 'fake machine image bytes\n' >"$DEST/machine/podman-machine.raw.zst"
chmod 0644 "$DEST/machine/podman-machine.raw.zst"

# A symlink, matching stage-runtime.sh's own bin/pasta -> bin/passt on Linux
# (the general fix is uniform across targets, not macOS-only).
printf 'fake passt\n' >"$DEST/bin/passt"
chmod 0755 "$DEST/bin/passt"
ln -s passt "$DEST/bin/pasta"

echo "[*] precondition: 3 files are genuinely not owner-writable before normalizing"
for f in bin/krunkit lib/libkrun.dylib lib/libMoltenVK.dylib lib/libvirglrenderer.1.dylib; do
  [ -w "$DEST/$f" ] && fail "$f was already owner-writable — the test fixture is wrong"
done
[ -L "$DEST/bin/pasta" ] || fail "bin/pasta should start as a symlink"

normalize_staged_tree "$DEST" || fail "normalize_staged_tree itself returned non-zero"

echo "[*] every file is now owner-writable (the actual xattr precondition)"
while IFS= read -r -d '' f; do
  [ -w "$f" ] || fail "$f is still not owner-writable after normalizing"
done < <(find "$DEST" -type f -print0)

echo "[*] executables are 0755, everything else is 0644"
_mode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%OLp' "$1"; }
for f in bin/krunkit bin/podman bin/passt bin/pasta; do
  [ "$(_mode "$DEST/$f")" = "755" ] || fail "$f: mode $(_mode "$DEST/$f"), want 755"
done
for f in lib/libkrun.dylib lib/libMoltenVK.dylib lib/libvirglrenderer.1.dylib lib/libepoxy.0.dylib \
         share/krunkit/KRUN_EFI.silent.fd machine/podman-machine.raw.zst; do
  [ "$(_mode "$DEST/$f")" = "644" ] || fail "$f: mode $(_mode "$DEST/$f"), want 644"
done

echo "[*] directories are 0755"
while IFS= read -r -d '' d; do
  [ "$(_mode "$d")" = "755" ] || fail "$d: mode $(_mode "$d"), want 755"
done < <(find "$DEST" -type d -print0)

echo "[*] no symlinks remain, and the flattened copy has the resolved content + right mode"
[ -L "$DEST/bin/pasta" ] && fail "bin/pasta is still a symlink after normalizing"
[ -f "$DEST/bin/pasta" ] || fail "bin/pasta must exist as a regular file after flattening"
[ "$(cat "$DEST/bin/pasta")" = "fake passt" ] || fail "bin/pasta's content does not match passt's"
SYMLINK_COUNT="$(find "$DEST" -type l | wc -l | tr -d ' ')"
[ "$SYMLINK_COUNT" = 0 ] || fail "$SYMLINK_COUNT symlink(s) remain"

if [ "$(uname -s)" = Darwin ]; then
  echo "[*] macOS host: xattr -cr was invoked on \$DEST (function returned 0, which requires it to have succeeded)"
else
  echo "[i] non-Darwin host ($(uname -s)): the xattr -cr branch does not run here (guarded on uname -s = Darwin," \
       "matching stage-runtime.sh's own macOS-only staging guard) — permissions/symlinks are what this host CAN verify"
fi

echo "[ok] all normalize_staged_tree assertions passed"
