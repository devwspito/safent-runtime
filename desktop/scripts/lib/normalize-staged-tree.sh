#!/usr/bin/env bash
# normalize-staged-tree.sh — one function, `normalize_staged_tree`, run on a
# target's staged directory after every staging step. Sourced, never
# executed directly.
#
#   normalize_staged_tree <dest-dir>
#
# Exists because of a REAL signed-pipeline failure on macOS (run 34523410889,
# the first run to reach bundling with a staged runtime): tauri-bundler died
# with "failed to bundle project: failed to remove extra attributes from app
# bundle: failed to run xattr". Root cause, confirmed by reading
# tauri-bundler's own source (crates/tauri-bundler/src/bundle/macos/app.rs,
# `remove_extra_attr`, 2.9.4): once every resource is copied into the .app,
# it runs `xattr -crs <bundle>` — recursively, on the WHOLE bundle — and
# `xattr -c` cannot clear attributes on a file it does not have WRITE
# permission to. The official krunkit v1.3.2 release tarball itself ships
# `bin/krunkit` at 0555 and THREE of its four .dylib files
# (libkrun.dylib, libMoltenVK.dylib, libvirglrenderer.1.dylib) at 0444 — no
# owner-write bit at all — and `cp -p` (used to preserve timestamps) carries
# those exact read-only bits into the staged tree. stage-runtime.sh already
# `chmod 0755`'d the four bin/ executables it explicitly names, but never
# touched the .dylib files it globs in — confirmed via a fresh extraction of
# the real v1.3.2 tarball (see this fix's commit message for the exact
# `tar tzv` output). NOT the machine image: it is written fresh by
# `curl -o` (see fetch-verified.sh), which gets the umask's default ~0644 —
# already owner-writable — and xattr has no documented file-size limit that
# 932 MB would approach.
#
# So this normalizes EVERY staged tree, every target, unconditionally:
#   - directories -> 0755
#   - files -> 0644, except a known set of executable basenames -> 0755
#   - symlinks -> replaced by a real copy of what they resolve to (not the
#     confirmed cause here — this krunkit release ships none — but the
#     STRUCTURAL fix for "does xattr's -s / a broken/relative link surprise
#     us" is removing the whole class, not reasoning about each instance)
#   - on macOS, finally runs `xattr -cr` on the staged dir itself, so any
#     quarantine/provenance attribute this build machine itself attached
#     during download/extraction is already gone before tauri-bundler ever
#     touches the tree, instead of only fixed by getting that far unbroken.
set -uo pipefail

# Keep the pinned Compose provider executable through normalization, before the
# manifest records permissions and before codesign seals the final app.
_EXECUTABLE_BASENAMES="podman docker-compose crun fuse-overlayfs fusermount3 passt pasta gvproxy vfkit krunkit conmon netavark aardvark-dns rootlessport catatonit safent run-safent.sh provision.sh"

normalize_staged_tree() {
  local dest="$1"
  [ -d "$dest" ] || { echo "[x] normalize_staged_tree: not a directory: $dest" >&2; return 1; }

  find "$dest" -type d -exec chmod 0755 {} +

  local f base is_exe name
  while IFS= read -r -d '' f; do
    base="$(basename "$f")"
    is_exe=0
    for name in $_EXECUTABLE_BASENAMES; do
      [ "$base" = "$name" ] && { is_exe=1; break; }
    done
    if [ "$is_exe" -eq 1 ]; then
      chmod 0755 "$f"
    else
      chmod 0644 "$f"
    fi
  done < <(find "$dest" -type f -print0)

  # Symlinks: not caught by -type f above (find's -type reports the entry's
  # OWN type, never the type it resolves to) — replace each with a real file.
  local link target
  while IFS= read -r -d '' link; do
    target="$(readlink -f "$link" 2>/dev/null || readlink "$link")"
    if [ -z "$target" ] || [ ! -e "$target" ]; then
      echo "[x] normalize_staged_tree: broken symlink $link -> ${target:-?}" >&2
      return 1
    fi
    rm -f "$link"
    cp -p "$target" "$link"
    base="$(basename "$link")"
    is_exe=0
    for name in $_EXECUTABLE_BASENAMES; do
      [ "$base" = "$name" ] && { is_exe=1; break; }
    done
    chmod "$([ "$is_exe" -eq 1 ] && echo 0755 || echo 0644)" "$link"
  done < <(find "$dest" -type l -print0)

  if [ "$(uname -s)" = Darwin ] && command -v xattr >/dev/null 2>&1; then
    xattr -cr "$dest" || { echo "[x] normalize_staged_tree: 'xattr -cr $dest' failed" >&2; return 1; }
  fi

  return 0
}
