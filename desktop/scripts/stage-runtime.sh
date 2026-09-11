#!/usr/bin/env bash
# stage-runtime.sh — download the PINNED podman runtime for one target and stage
# it into desktop/src-tauri/resources/runtime/<target>/, where Tauri's resource
# resolver (tauri::Manager::path().resource_dir()) makes it available to the app
# at runtime. This is a BUILD-TIME script (developer machine or CI runner) — not
# shipped to end users, unlike `safent`/`get-safent.sh` (kept POSIX sh for that
# reason). This one may assume bash + curl + jq + sha256sum/shasum + tar.
#
# Usage:
#   desktop/scripts/stage-runtime.sh <target>
#   target = a RUST TARGET TRIPLE — the SAME ones agents-autonomy/.github/
#   workflows/safent-desktop.yml already uses in its build matrix, so the
#   pipeline can call this script with no translation layer of its own:
#     aarch64-apple-darwin        macOS Apple Silicon (ships a VM machine image)
#     x86_64-unknown-linux-gnu    Linux x86_64 (no VM)
#     aarch64-unknown-linux-gnu   Linux arm64 (no VM)
#   x86_64-apple-darwin is a RECOGNIZED triple that is deliberately rejected
#   (see below) — everything else is an unrecognized triple, rejected loudly.
#
# Optional per-build digest injection (both env vars, see lib/
# resolve-image-digest.sh for the exact override-or-match contract):
#   SAFENT_ENGINE_DIGEST      sha256:<64 lowercase hex> — the engine
#                             container image the release actually published
#   SAFENT_COMPANION_DIGEST   same format — the companion image, if any
# Both OVERRIDE runtime-manifest.lock's engine_image/companion_image.digest
# ONLY when the lock has null (not yet pinned); if the lock already pins a
# digest, the env var MUST match it exactly or this script exits EXIT_USAGE
# — a released, pinned build can never be silently repointed at a different
# image by an env var. Recorded in runtime-bundle.json alongside a
# `platform` field (linux/arm64 or linux/amd64 — the Linux VM architecture
# THIS target's engine container actually runs, see
# lib/resolve-image-digest.sh's platform_for_target) so boot.rs can refuse a
# digest recorded for the wrong platform instead of trusting it blindly.
# `--refresh-bundle-json <target>` (below) preserves whatever digest an
# earlier staging run already recorded even if THIS invocation re-exports
# neither env var — see _write_runtime_bundle_manifest's own fallback.
#
# Exit codes (every non-zero exit in this script and in lib/fetch-verified.sh
# uses one of these — "the install never fails" means failing LOUDLY and
# distinguishably, not silently or ambiguously):
#   0  success (including "already staged, nothing to do")
#   1  usage error: bad/missing target argument, or a required tool
#      (curl/jq/tar/pkgutil) is missing from PATH
#   2  download failed: the network never delivered a complete transfer
#      despite every retry (EXIT_DOWNLOAD, see lib/fetch-verified.sh)
#   3  integrity failure: a transfer completed (right byte count) but its
#      sha256 did not match the pinned lock, twice in a row — corruption,
#      not incompleteness (EXIT_INTEGRITY, see lib/fetch-verified.sh)
#   4  staging failure: something in the lock file, an archive's layout, or
#      a .pkg's payload did not match what this script expects (not a
#      network problem — retrying would not help)
#   5  platform guard: this target must be staged on a different host OS
#      (aarch64-apple-darwin needs pkgutil, i.e. an actual macOS host/runner)
#
# Every URL + sha256 this script trusts comes from ONE committed file,
# desktop/runtime-manifest.lock (sibling of this script's parent dir, keyed by
# the SAME triples) — nothing is fetched from a floating "latest" endpoint. A
# downloaded byte that does not match its pinned sha256 is deleted and the
# script exits non-zero: a mismatched binary is NEVER staged, let alone
# executed (data-model.md RuntimeBundle invariant — this script is the FIRST
# of two checks; the app re-verifies at runtime before exec, see
# contracts/app-engine.md).
#
# Every download (archives AND the machine image) goes through
# lib/fetch-verified.sh's `fetch_verified`: resumable across both curl's own
# --retry budget and dropped connections between separate runs of this
# script (a killed/retried CI job resumes instead of restarting an 888 MiB
# transfer from zero — the exact failure a real macOS pipeline run hit
# before this existed: a connection closed 31 MB from the end and the old,
# non-resuming logic threw the whole download away).
#
# aarch64-apple-darwin must run on a macOS host/runner: it expands the official
# podman .pkg with `pkgutil --expand-full` WITHOUT installing it (no
# `installer -pkg`, no admin password) and copies the binaries out. On Linux
# this step fails fast with a clear message instead of doing partial work.
set -euo pipefail

EXIT_USAGE=1
EXIT_STAGE=4
EXIT_PLATFORM=5

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DESKTOP_DIR/.." && pwd)"
LOCKFILE="$DESKTOP_DIR/runtime-manifest.lock"
RESOURCES_ROOT="$DESKTOP_DIR/src-tauri/resources/runtime"
# Outside resources/runtime/ ENTIRELY, on purpose (see CACHE_DIR below): a
# real macOS notarization run (notarytool) rejected the app because raw
# download archives (krunkit-podman-unsigned-1.3.2.tgz) were being bundled
# alongside their own extracted/signed contents — Apple's tooling opens
# archives and inspects what's inside them too. bundle.resources' own glob
# is `resources/runtime/*/**/*`, which matches ANY first path segment
# including a dotdir (the `glob` crate does not exclude dot-entries from `*`
# by default) — so a cache nested ANYWHERE under resources/runtime/, even in
# a gitignored dotdir, still gets swept into the .app. Never put download
# state under resources/runtime/ again; see tests/test-packaged-layout.sh.
# CACHE_DIR itself is assigned below, once $TARGET is validated (it is
# per-target: $DESKTOP_DIR/.cache/runtime-downloads/$TARGET).

# The `safent` CLI + its host launcher + the companion's provisioning assets
# are PART OF THIS REPO (unlike podman: a pinned third party) — no network
# fetch, no upstream sha256 to trust ahead of time. `cmd_stage_runtime`
# (`safent`) resolves the CLI as `runtime_dir.join("safent")`
# (`boot.rs::resolve_config_with_fallback`) and needs `runtime-bundle.json`
# right next to itself to do anything at all (data-model.md RuntimeBundle:
# "un binario que no verifica no se ejecuta jamás" applies here too, not
# only to podman) — without these, the packaged app has no CLI to invoke,
# full stop (see specs/028-safent-app-nativa/verificacion-paquete-linux.md
# §"Comprobación 2"). Paths are repo-root-relative; staged FLAT (basename),
# matching the same glob-flattened `resources/runtime/*/**/*` convention
# every podman file already uses (RUNTIME-BUNDLE.md).
# MAC3-03 (verificacion-mac-3.md, MAC2-07 repeated unfixed): ops/container/
# seccomp/safent.json is the SAME file ops/container/Containerfile bakes
# into the image at /usr/share/hermes/seccomp/safent.json — bundling it
# here too means `safent`'s _ensure_seccomp can resolve it straight from
# the packaged, hash-verified resources sitting beside the pinned podman
# binary, with no network fetch and no dependency on the image already
# being pulled (both of which can legitimately fail on first boot, and
# both of which stage a copy under $SAFENT_STATE_HOME — a host path that
# must be VM-visible on macOS, see safent's own SAFENT_STATE_HOME
# canonicalization comment).
APP_FILES=(
  "safent"
  "ops/container/run-safent.sh"
  "ops/container/seccomp/safent.json"
  "ops/container/companions/ads/provision.sh"
  "ops/container/companions/ads/compose.yaml"
  "ops/container/companions/ads/caps.template.yaml"
)

# shellcheck source=lib/fetch-verified.sh
source "$SCRIPT_DIR/lib/fetch-verified.sh"
# shellcheck source=lib/normalize-staged-tree.sh
source "$SCRIPT_DIR/lib/normalize-staged-tree.sh"
# shellcheck source=lib/patch-containers-conf.sh
source "$SCRIPT_DIR/lib/patch-containers-conf.sh"
# shellcheck source=lib/resolve-image-digest.sh
source "$SCRIPT_DIR/lib/resolve-image-digest.sh"

# MAC-02 (verificacion-mac-1.md): the signing pipeline re-writes every
# Mach-O's bytes AFTER this script normally staged+hashed them (cdhash
# identity, sha256 no longer matches — same failure the lock already
# documents for AppImage, now hitting the notarized DMG, the primary
# delivery). `--refresh-bundle-json <target>` re-scans an ALREADY-staged
# $DEST in place and rewrites runtime-bundle.json (fresh sha256 + a real
# cdhash for every Mach-O, via `_write_runtime_bundle_manifest` — defined
# below, actually INVOKED near the bottom of this script once every
# function it needs exists) — downloads NOTHING; the pipeline calls it
# once, right after codesign, never before staging has happened for real.
# Argument validation happens NOW (fail fast on bad usage); $DEST's
# existence is re-checked at the bottom, right before the actual call,
# since nothing between here and there may create it in refresh mode.
REFRESH_ONLY=0
if [ "${1:-}" = "--refresh-bundle-json" ]; then
  REFRESH_ONLY=1
  TARGET="${2:-}"
  case "$TARGET" in
    aarch64-apple-darwin|x86_64-unknown-linux-gnu|aarch64-unknown-linux-gnu) ;;
    *)
      echo "[x] usage: $0 --refresh-bundle-json <target> [dir]" >&2
      echo "    target = aarch64-apple-darwin | x86_64-unknown-linux-gnu | aarch64-unknown-linux-gnu" >&2
      exit "$EXIT_USAGE"
      ;;
  esac
  [ -f "$LOCKFILE" ] || { echo "[x] missing $LOCKFILE" >&2; exit "$EXIT_USAGE"; }
  # MAC2-08 (verificacion-mac-2.md): a real signed DMG shipped with all 15
  # cdhash entries null despite sha256 matching post-sign — codesigning
  # runs against the BUILT .app's OWN copy of these files
  # (Contents/Resources/runtime/, produced by Tauri's bundle.resources
  # COPYING resources/runtime/<triple>/ at build time), never against
  # THIS staging directory, which nothing touches again after
  # stage-runtime.sh's own normal run. Refreshing $RESOURCES_ROOT/$TARGET
  # unconditionally could only ever re-hash the UNSIGNED staging copy —
  # right sha256 (nothing there changed), permanently null cdhash (nothing
  # there was ever signed). An optional 3rd arg lets the pipeline point
  # this at the ACTUAL signed location once it exists.
  if [ -n "${3:-}" ]; then
    DEST="$3"
  else
    DEST="$RESOURCES_ROOT/$TARGET"
  fi
  [ -d "$DEST" ] || {
    echo "[x] $DEST does not exist — stage $TARGET normally first; this mode never downloads" >&2
    exit "$EXIT_USAGE"
  }
fi

[ "$REFRESH_ONLY" -eq 1 ] || TARGET="${1:-}"
case "$TARGET" in
  aarch64-apple-darwin|x86_64-unknown-linux-gnu|aarch64-unknown-linux-gnu) ;;
  x86_64-apple-darwin)
    echo "[x] x86_64-apple-darwin: not staged on purpose — v6.1.1 publishes no" >&2
    echo "    macOS Intel installer. Matches contracts/update.md (darwin-x86_64" >&2
    echo "    intentionally absent). See runtime-manifest.lock ->" >&2
    echo "    excluded_targets.x86_64-apple-darwin." >&2
    exit "$EXIT_USAGE"
    ;;
  "")
    echo "usage: $0 <aarch64-apple-darwin|x86_64-unknown-linux-gnu|aarch64-unknown-linux-gnu>" >&2
    exit "$EXIT_USAGE"
    ;;
  *)
    echo "[x] unrecognized target triple: $TARGET" >&2
    echo "    want one of: aarch64-apple-darwin | x86_64-unknown-linux-gnu | aarch64-unknown-linux-gnu" >&2
    exit "$EXIT_USAGE"
    ;;
esac

for tool in curl jq tar; do
  command -v "$tool" >/dev/null 2>&1 || { echo "[x] need '$tool' on PATH (build-time only, not shipped)" >&2; exit "$EXIT_USAGE"; }
done

[ -f "$LOCKFILE" ] || { echo "[x] missing $LOCKFILE" >&2; exit "$EXIT_USAGE"; }
# --refresh-bundle-json touches no pkgutil/download logic at all (it only
# re-hashes+cdhashes files ALREADY staged by a real prior macOS run) — the
# platform guard below is for the NORMAL staging path only, so this mode's
# own logic stays testable on any host (it depends on $CODESIGN being
# present, checked lazily, only if a Mach-O file is actually found).
[ "$REFRESH_ONLY" -eq 1 ] || case "$TARGET" in
  *-apple-darwin)
    if [ "$(uname -s)" != Darwin ]; then
      echo "[x] $TARGET must be staged on a macOS host/runner (uses pkgutil to" >&2
      echo "    expand the official .pkg WITHOUT installing it). Refusing to do" >&2
      echo "    partial work on $(uname -s)." >&2
      exit "$EXIT_PLATFORM"
    fi
    command -v pkgutil >/dev/null 2>&1 || { echo "[x] need 'pkgutil' (macOS only) on PATH" >&2; exit "$EXIT_USAGE"; }
    ;;
esac

[ "$REFRESH_ONLY" -eq 1 ] || DEST="$RESOURCES_ROOT/$TARGET"
# Persistent across runs (on purpose — a killed script resumes an in-flight
# archive/image download from here instead of restarting it): gitignored,
# but NOT under resources/runtime/ — see the comment on RESOURCES_ROOT above.
# The pipeline lane's `actions/cache` step must key/cache THIS path, not the
# old resources/runtime/.cache/ one.
CACHE_DIR="$DESKTOP_DIR/.cache/runtime-downloads/$TARGET"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM

echo "[*] stage-runtime: target=$TARGET lock=$LOCKFILE dest=$DEST"

# ---- already staged with matching hashes? skip the network entirely -----------
_already_staged() {
  jq -e '.targets[$t].entries // empty' --arg t "$TARGET" "$LOCKFILE" >/dev/null 2>&1 || return 1
  local n
  n="$(jq -r '.targets[$t].entries | length' --arg t "$TARGET" "$LOCKFILE")"
  [ "$n" -gt 0 ] || return 1
  local i path want_sha got_sha
  for ((i = 0; i < n; i++)); do
    path="$(jq -r ".targets[\$t].entries[$i].path" --arg t "$TARGET" "$LOCKFILE")"
    want_sha="$(jq -r ".targets[\$t].entries[$i].sha256" --arg t "$TARGET" "$LOCKFILE")"
    # etc/containers/containers.conf is rewritten in place by
    # _patch_containers_conf after extraction (see there) — on disk it is
    # NEVER the pristine upstream original this entry's own hash pins, so
    # check it against containers_conf_patch's hash instead (the hash of
    # the FULL patched content — helper_binaries_dir + default_rootless_
    # network_cmd + lock_type together — so a match here already proves
    # every one of those three landed correctly, not just one of them).
    # macOS has no such entry at all, hence the existence check.
    if [ "$path" = "etc/containers/containers.conf" ] && \
       jq -e '.targets[$t].containers_conf_patch // empty' --arg t "$TARGET" "$LOCKFILE" >/dev/null 2>&1; then
      want_sha="$(jq -r '.targets[$t].containers_conf_patch.sha256' --arg t "$TARGET" "$LOCKFILE")"
    fi
    [ -f "$DEST/$path" ] || return 1
    got_sha="$(SHA256 "$DEST/$path")"
    [ "$got_sha" = "$want_sha" ] || return 1
  done
  # App files are repo content, not a frozen download — "already staged"
  # must mean "matches THIS checkout right now", so an edited safent/
  # provision.sh always gets re-staged instead of silently going stale.
  [ -f "$DEST/runtime-bundle.json" ] || return 1
  local rel base
  for rel in "${APP_FILES[@]}"; do
    base="$(basename "$rel")"
    [ -f "$DEST/$base" ] || return 1
    [ "$(SHA256 "$REPO_ROOT/$rel")" = "$(SHA256 "$DEST/$base")" ] || return 1
  done
  return 0
}

# --refresh-bundle-json re-scans whatever a prior REAL staging run already
# left under $DEST (now signed, out-of-band) — wiping it here would defeat
# the entire point ("no re-downloading").
if [ "$REFRESH_ONLY" -eq 0 ]; then
  if _already_staged; then
    echo "[ok] $TARGET already staged under $DEST with matching sha256 for every entry — skipping download."
    exit 0
  fi
  rm -rf "$DEST"
  mkdir -p "$DEST" "$CACHE_DIR"
fi

# ---- x86_64/aarch64-unknown-linux-gnu: static tarball, extract a curated subset --
_stage_linux() {
  local url want_sha want_size archive n i
  url="$(jq -r '.targets[$t].download.url' --arg t "$TARGET" "$LOCKFILE")"
  want_sha="$(jq -r '.targets[$t].download.sha256' --arg t "$TARGET" "$LOCKFILE")"
  want_size="$(jq -r '.targets[$t].download.size_bytes' --arg t "$TARGET" "$LOCKFILE")"
  archive="$CACHE_DIR/$(basename "$url")"
  echo "    downloading $(basename "$url") ($((want_size / 1024 / 1024)) MiB)..."
  fetch_verified "$url" "$archive" "$want_size" "$want_sha" || exit $?
  echo "    verified sha256 $want_sha"

  # The tarball's own top-level dir varies only by arch name; discover it instead
  # of hardcoding "podman-linux-<arch>/" so a future archive layout tweak upstream
  # doesn't silently no-op every -C extraction below.
  # (subshell + pipefail off: `head -1` closing its input early after the first
  # line otherwise SIGPIPEs `tar tzf` and, under `set -o pipefail`, aborts the
  # whole script via `set -e` even though this line did exactly what it should.)
  local top
  top="$(set +o pipefail; tar tzf "$archive" | head -1)"

  n="$(jq -r '.targets[$t].entries | length' --arg t "$TARGET" "$LOCKFILE")"
  for ((i = 0; i < n; i++)); do
    local path want_bin_sha want_bin_size mode member
    path="$(jq -r ".targets[\$t].entries[$i].path" --arg t "$TARGET" "$LOCKFILE")"
    want_bin_sha="$(jq -r ".targets[\$t].entries[$i].sha256" --arg t "$TARGET" "$LOCKFILE")"
    want_bin_size="$(jq -r ".targets[\$t].entries[$i].size_bytes" --arg t "$TARGET" "$LOCKFILE")"
    mode="$(jq -r ".targets[\$t].entries[$i].mode" --arg t "$TARGET" "$LOCKFILE")"
    case "$path" in
      libexec/podman/*) member="${top}usr/local/lib/podman/${path#libexec/podman/}" ;;
      bin/*)            member="${top}usr/local/bin/${path#bin/}" ;;
      etc/*)             member="${top}${path}" ;;
      *) echo "[x] unexpected entry path in lock file: $path" >&2; exit "$EXIT_STAGE" ;;
    esac
    mkdir -p "$WORK/x" "$(dirname "$DEST/$path")"
    tar xzf "$archive" -C "$WORK/x" "$member"
    mv "$WORK/x/$member" "$DEST/$path"
    rm -rf "${WORK:?}/x"
    chmod "$mode" "$DEST/$path"
    local got_bin_sha got_bin_size
    got_bin_sha="$(SHA256 "$DEST/$path")"
    got_bin_size="$(_filesize "$DEST/$path")"
    if [ "$got_bin_sha" != "$want_bin_sha" ] || [ "$got_bin_size" != "$want_bin_size" ]; then
      rm -f "$DEST/$path"
      echo "[x] staged $path does not match runtime-manifest.lock (sha256 or size) — refusing to keep it." >&2
      exit "$EXIT_STAGE"
    fi
    echo "    staged $path ($got_bin_size bytes, sha256 verified)"
  done

  # podman's rootless network backend looks for a binary literally named
  # 'pasta' (containers/common default); upstream ships it as passt+symlink.
  ln -sf passt "$DEST/bin/pasta"
  echo "    linked bin/pasta -> bin/passt"

  _patch_containers_conf
}

# containers.conf, as extracted above (verified against its OWN entries[]
# sha256 — the pristine upstream original), needs TWO independent fixes
# before it is fit to ship, found by two different lanes against the same
# file:
#
# 1. It does not point podman at ITS OWN bundled netavark/aardvark-dns/
#    rootlessport: without this, a host that happens to already have podman
#    installed silently uses THAT copy instead (confirmed on this DGX:
#    podman fell back to the system's netavark 1.4.0 instead of the bundled
#    2.1.0) — exactly the "depends on what happens to already be on the
#    machine" failure the whole point of bundling exists to avoid. $BINDIR
#    is containers-common's OWN token (pkg/config/config.go,
#    FindHelperBinary) for "the directory containing the CURRENTLY RUNNING
#    podman binary", resolved fresh at runtime via os.Executable() — not
#    baked in at stage time, so the same staged file is correct BOTH here
#    (resources/runtime/<target>/) and wherever the app later deploys the
#    bundle for real (~/.safent/runtime/<version>/, per data-model.md).
#    default_rootless_network_cmd = "pasta" is already podman 6.1.1's own
#    default (RELEASE_NOTES.md) — set explicitly anyway so the product does
#    not silently change behavior if a future podman release changes it.
# 2. A bundled STATIC (musl) podman and the host's own (glibc) podman/
#    docker, run as the same uid, collide on ONE shared /dev/shm rootless
#    lock segment sized differently by each libc's pthread_mutex_t layout —
#    reproduced live: "failed to open 2048 locks in
#    /libpod_rootless_lock_1000: numerical result out of range"
#    (specs/028-safent-app-nativa/verificacion-paquete-linux.md §"Pasada
#    1"). `lock_type = "file"` switches to per-storage-tree file locks —
#    no segment shared by uid at all, so no collision is possible.
#
# Both append into the SAME [engine] section (via the sed `a` command,
# which inserts immediately after the `[engine]` header regardless of what
# a previous call already inserted there) before the network-cmd fix adds
# its own new [network] section at the very end — order between the two
# [engine] keys does not matter, but both MUST land before any later
# section header or they would silently become that section's keys
# instead. Appends, never overwrites the pre-existing keys — the whole
# result is verified against its OWN pinned hash (containers_conf_patch in
# the lock file), separate from the upstream original's entries[] hash,
# because this is OUR generated content, not a re-check of the same bytes.
_patch_containers_conf() {
  local conf="$DEST/etc/containers/containers.conf"
  local want_sha want_size
  want_sha="$(jq -r '.targets[$t].containers_conf_patch.sha256' --arg t "$TARGET" "$LOCKFILE")"
  want_size="$(jq -r '.targets[$t].containers_conf_patch.size_bytes' --arg t "$TARGET" "$LOCKFILE")"

  patch_containers_conf_for_bundled_helpers "$conf" || exit "$EXIT_STAGE"
  patch_containers_conf_for_isolated_locks "$conf" || exit "$EXIT_STAGE"

  local got_sha got_size
  got_sha="$(SHA256 "$conf")"
  got_size="$(_filesize "$conf")"
  if [ "$got_sha" != "$want_sha" ] || [ "$got_size" != "$want_size" ]; then
    rm -f "$conf"
    echo "[x] patched containers.conf does not match runtime-manifest.lock's containers_conf_patch (sha256 or size) — refusing to keep it." >&2
    exit "$EXIT_STAGE"
  fi
  echo "    patched etc/containers/containers.conf (helper_binaries_dir + default_rootless_network_cmd + lock_type, $got_size bytes, sha256 verified)"
}

# ---- aarch64-apple-darwin: expand the official .pkg (never install it) + VM ---
_stage_macos() {
  local url want_sha want_size pkg expanded
  url="$(jq -r '.targets[$t].download.url' --arg t "$TARGET" "$LOCKFILE")"
  want_sha="$(jq -r '.targets[$t].download.sha256' --arg t "$TARGET" "$LOCKFILE")"
  want_size="$(jq -r '.targets[$t].download.size_bytes' --arg t "$TARGET" "$LOCKFILE")"
  pkg="$CACHE_DIR/$(basename "$url")"
  echo "    downloading $(basename "$url") ($((want_size / 1024 / 1024)) MiB)..."
  fetch_verified "$url" "$pkg" "$want_size" "$want_sha" || exit $?
  echo "    verified sha256 $want_sha"

  expanded="$WORK/pkg-expanded"
  rm -rf "$expanded"
  pkgutil --expand-full "$pkg" "$expanded"

  mkdir -p "$DEST/bin"
  local found
  for bin in podman gvproxy vfkit; do
    found="$(set +o pipefail; find "$expanded" -type f -name "$bin" -perm -u+x | head -1)"
    [ -n "$found" ] || { echo "[x] '$bin' not found inside the expanded .pkg payload" >&2; exit "$EXIT_STAGE"; }
    cp -p "$found" "$DEST/bin/$bin"
    chmod 0755 "$DEST/bin/$bin"
    echo "    staged bin/$bin ($(SHA256 "$DEST/bin/$bin"))"
  done

  # krunkit: alternative (libkrun/GPU) machine provider, bundled so the app can
  # start a pre-existing libkrun machine it adopts (see T024 quickstart) without
  # requiring the owner to already have krunkit installed.
  local kurl ksha ksize karchive kroot
  kurl="$(jq -r '.targets[$t].krunkit.download.url' --arg t "$TARGET" "$LOCKFILE")"
  ksha="$(jq -r '.targets[$t].krunkit.download.sha256' --arg t "$TARGET" "$LOCKFILE")"
  ksize="$(jq -r '.targets[$t].krunkit.download.size_bytes' --arg t "$TARGET" "$LOCKFILE")"
  karchive="$CACHE_DIR/$(basename "$kurl")"
  echo "    downloading $(basename "$kurl") ($((ksize / 1024 / 1024)) MiB)..."
  fetch_verified "$kurl" "$karchive" "$ksize" "$ksha" || exit $?
  echo "    verified sha256 $ksha"
  kroot="$WORK/krunkit"
  mkdir -p "$kroot"
  tar xzf "$karchive" -C "$kroot"
  cp -p "$kroot/bin/krunkit" "$DEST/bin/krunkit"
  chmod 0755 "$DEST/bin/krunkit"
  mkdir -p "$DEST/lib" "$DEST/share/krunkit"
  cp -p "$kroot"/lib/*.dylib "$DEST/lib/"
  cp -p "$kroot/share/krunkit/KRUN_EFI.silent.fd" "$DEST/share/krunkit/"
  echo "    staged bin/krunkit + lib/*.dylib + share/krunkit/KRUN_EFI.silent.fd"

  # Machine image: pulled by BLOB DIGEST from the registry's content-addressable
  # blob store — the digest below IS the sha256 of the blob by OCI protocol
  # invariant, so this is a real verified-download, not a self-check. This is
  # the 888 MiB transfer that motivated fetch_verified's resume logic in the
  # first place (a real macOS run dropped 31 MB from the end of it).
  local oci_ref blob_digest blob_size blob_name
  oci_ref="$(jq -r '.targets[$t].machine_image.oci_ref' --arg t "$TARGET" "$LOCKFILE")"
  blob_digest="$(jq -r '.targets[$t].machine_image.blob_digest' --arg t "$TARGET" "$LOCKFILE")"
  blob_size="$(jq -r '.targets[$t].machine_image.size_bytes' --arg t "$TARGET" "$LOCKFILE")"
  blob_name="$(jq -r '.targets[$t].machine_image.blob_filename' --arg t "$TARGET" "$LOCKFILE")"
  local registry_host registry_repo
  registry_host="${oci_ref%%/*}"
  registry_repo="${oci_ref#*/}"; registry_repo="${registry_repo%%:*}"
  local blob_sha="${blob_digest#sha256:}"
  echo "    downloading machine image $blob_name ($((blob_size / 1024 / 1024)) MiB)..."
  fetch_verified "https://$registry_host/v2/$registry_repo/blobs/$blob_digest" \
    "$DEST/machine/$blob_name" "$blob_size" "$blob_sha" || exit $?
  echo "    verified machine image digest sha256:$blob_sha"
}

# ---- app files: the safent CLI + host launcher + companion provisioning ----
# assets, staged FLAT from this repo checkout (see APP_FILES's own comment
# above). No fetch_verified: these are local files, not a network transfer.
_stage_app_files() {
  local rel src base
  for rel in "${APP_FILES[@]}"; do
    src="$REPO_ROOT/$rel"
    [ -f "$src" ] || { echo "[x] missing app file in repo: $rel" >&2; exit "$EXIT_STAGE"; }
    base="$(basename "$rel")"
    cp -p "$src" "$DEST/$base"
    echo "    staged app file $base (from $rel, sha256 $(SHA256 "$DEST/$base"))"
  done
}

# `cmd_stage_runtime` (`safent`) reads THIS file, sitting beside itself once
# packaged+flattened, to know what to verify-and-copy into
# $SAFENT_STATE_HOME/runtime/<version>/ at first real run (data-model.md
# RuntimeBundle). Entries cover EVERY flat file this script staged — the
# podman toolchain (currently nested under bin/libexec/etc/, per
# runtime-manifest.lock, until Tauri's own bundle.resources glob flattens
# it at package time) AND the app files above — all addressed by their
# POST-FLATTEN flat basename, matching what `cmd_stage_runtime`'s own
# `bundle_dir` will actually contain at runtime.
#
# MAC-03 (verificacion-mac-1.md): also carries `engine_image`/
# `companion_image` straight from `runtime-manifest.lock`'s own top-level
# fields of the same name — the release pipeline's pinned repo+digest for
# the engine/companion CONTAINER images (a build-time fact, mirroring how
# `machine_image.manifest_digest` is already pinned by a human, never
# queried live here). `boot.rs`/`selftest.rs` read these from THIS shipped
# file instead of the `SAFENT_ENGINE_DIGEST` env var nothing in the real
# packaging pipeline ever sets. `null` (repo absent, or digest not yet
# pinned) ships through unchanged — a legitimate "not fixed yet" state the
# wrapper fails closed on, not a staging error.
# MAC-02 (verificacion-mac-1.md): a Mach-O's own bytes change the instant it
# is (re-)signed, so sha256 recorded here BEFORE signing (this script's
# normal run) stops matching the moment the pipeline's signing step
# rewrites the shipped binaries — the exact failure the lock already
# documents for AppImage, hitting the notarized DMG (the primary delivery)
# instead. Portable magic-byte sniff (no `file`/`lipo` dependency) so the
# SAME classification runs identically whether this script is staging a
# real macOS bundle or being exercised by a fixture-driven test on any
# other host — only the LATER `codesign` call (real signature/cdhash
# extraction, only reached for a file this classifies as Mach-O) is
# macOS-specific, and even that is overridable for tests (SAFENT_CODESIGN).
CODESIGN="${SAFENT_CODESIGN:-codesign}"
_is_macho() {
  magic="$(od -An -tx1 -N 4 "$1" 2>/dev/null | tr -d ' \n')"
  case "$magic" in
    cafebabe|feedface|cefaedfe|feedfacf|cffaedfe) return 0 ;;
    *) return 1 ;;
  esac
}

_write_runtime_bundle_manifest() {
  local podman_version out engine_repo engine_digest companion_repo companion_digest platform
  podman_version="$(jq -r '.targets[$t].podman_version' --arg t "$TARGET" "$LOCKFILE")"
  engine_repo="$(jq -r '.engine_image.repo // empty' "$LOCKFILE")"
  companion_repo="$(jq -r '.companion_image.repo // empty' "$LOCKFILE")"
  platform="$(platform_for_target "$TARGET")" || exit "$EXIT_USAGE"
  out="$DEST/runtime-bundle.json"

  # SAFENT_ENGINE_DIGEST/SAFENT_COMPANION_DIGEST: per-build injection for a
  # release the lock has not pinned yet (null) — overrides null, but a
  # digest the lock ALREADY pins must match exactly (mismatch = hard error,
  # see lib/resolve-image-digest.sh's own header for why). Validated+
  # resolved the same way whether this is a normal staging run or
  # --refresh-bundle-json, since both call this same function.
  engine_digest="$(resolve_image_digest "SAFENT_ENGINE_DIGEST" \
    "$(jq -r '.engine_image.digest // empty' "$LOCKFILE")" \
    "${SAFENT_ENGINE_DIGEST:-}")" || exit "$EXIT_USAGE"
  companion_digest="$(resolve_image_digest "SAFENT_COMPANION_DIGEST" \
    "$(jq -r '.companion_image.digest // empty' "$LOCKFILE")" \
    "${SAFENT_COMPANION_DIGEST:-}")" || exit "$EXIT_USAGE"

  # --refresh-bundle-json runs as its OWN process (a later pipeline step,
  # after signing) and may not re-export the same SAFENT_ENGINE_DIGEST/
  # SAFENT_COMPANION_DIGEST the original staging run used — with the lock
  # itself still null, both resolve empty above, and $out (about to be
  # overwritten below) is the ONLY remaining record of what was injected
  # the first time. A normal (non-refresh) run never reaches this with a
  # pre-existing $out: `_already_staged` either short-circuits before ever
  # calling this function, or `rm -rf "$DEST"` wiped it first — so this
  # only ever fires for a genuine re-scan of an already-staged tree, never
  # masks a fresh checkout's own missing digest with stale leftovers.
  if [ -f "$out" ]; then
    [ -n "$engine_digest" ] || engine_digest="$(jq -r '.engine_image.digest // empty' "$out" 2>/dev/null || true)"
    [ -n "$companion_digest" ] || companion_digest="$(jq -r '.companion_image.digest // empty' "$out" 2>/dev/null || true)"
  fi

  {
    find "$DEST" -type f ! -name 'runtime-bundle.json' -print0
  } | while IFS= read -r -d '' f; do
    b="$(basename "$f")"
    s="$(SHA256 "$f")"
    m="$(stat -c '%a' "$f" 2>/dev/null || stat -f '%Lp' "$f")"
    cdhash=null
    if _is_macho "$f"; then
      command -v "$CODESIGN" >/dev/null 2>&1 \
        || { echo "[x] '$CODESIGN' not found on PATH — needed to classify Mach-O file $b" >&2; exit "$EXIT_STAGE"; }
      # Not-yet-signed (a local dev build that never runs the signing
      # pipeline) reports nothing here on purpose — cdhash stays null and
      # cmd_stage_runtime falls back to sha256, still accurate for THAT
      # exact, untouched file.
      # `|| true` on the WHOLE pipeline (not just the last stage) — with
      # pipefail (this script's own `set -eo pipefail`), an unsigned file's
      # `codesign -dvvv` exiting non-zero would otherwise abort the ENTIRE
      # script right here instead of gracefully falling back to null.
      real_cdhash="$("$CODESIGN" -dvvv "$f" 2>/dev/null | sed -n 's/^CDHash=\(.*\)$/\1/p' | head -1 || true)"
      # `if`, not a standalone `&&` — under `set -e`, the common "not yet
      # signed" case (real_cdhash empty, the condition below is false)
      # would otherwise abort the WHOLE script right here (a bare `test &&
      # assignment` statement's own exit status is the test's, and `&&`
      # short-circuiting on false is exactly what set -e treats as a
      # command failure) — caught by this MAC-02 test's own "unsigned
      # Mach-O" case.
      if [ -n "$real_cdhash" ]; then
        cdhash="\"$real_cdhash\""
      fi
    fi
    printf '{"path":"%s","sha256":"%s","cdhash":%s,"mode":"0%s"}\n' "$b" "$s" "$cdhash" "$m"
  done | jq -s \
    --arg v "$podman_version" \
    --arg er "$engine_repo" --arg ed "$engine_digest" \
    --arg cr "$companion_repo" --arg cd "$companion_digest" \
    --arg p "$platform" \
    '{
      podman_version: $v,
      entries: .,
      engine_image: (if $er == "" then null
                      else {repo: $er, digest: (if $ed == "" then null else $ed end), platform: $p} end),
      companion_image: (if $cr == "" then null
                          else {repo: $cr, digest: (if $cd == "" then null else $cd end), platform: $p} end)
    }' > "$out"
  chmod 0644 "$out"
  echo "    wrote runtime-bundle.json ($(jq '.entries | length' "$out") entries)"
}

# "hashes recorded in runtime-manifest.lock" (packaging review item 1) — a
# DIFFERENT contract than targets[].entries: those are a third party,
# pinned once and verified against upstream by a human; app_files are OUR
# OWN code, so this script recomputes and overwrites them on every run from
# THIS checkout — self-updating provenance, not a value to hand-edit.
_record_app_files_in_lock() {
  local rel base sha size mode entries tmp
  entries="[]"
  for rel in "${APP_FILES[@]}"; do
    base="$(basename "$rel")"
    sha="$(SHA256 "$REPO_ROOT/$rel")"
    size="$(_filesize "$REPO_ROOT/$rel")"
    # The NORMALIZED mode (matches normalize-staged-tree.sh's
    # _EXECUTABLE_BASENAMES), not the repo checkout's own raw mode (which
    # can be group-writable, umask-dependent, etc.) — this is what
    # runtime-bundle.json actually records and cmd_stage_runtime applies.
    case "$base" in
      *.sh|safent) mode="0755" ;;
      *) mode="0644" ;;
    esac
    entries="$(jq --arg p "$rel" --arg f "$base" --arg s "$sha" --argjson sz "$size" --arg m "$mode" \
      '. + [{repo_path: $p, flat_name: $f, sha256: $s, size_bytes: $sz, mode: $m}]' <<<"$entries")"
  done
  tmp="$(mktemp)"
  jq --argjson e "$entries" \
    '.app_files = {note: "safent CLI + host launcher + companion provisioning assets — REPO content, recomputed by stage-runtime.sh from the current checkout on every run, never hand-pinned like targets[].entries.", entries: $e}' \
    "$LOCKFILE" > "$tmp"
  mv "$tmp" "$LOCKFILE"
}

if [ "$REFRESH_ONLY" -eq 1 ]; then
  # $DEST was NOT wiped (see the `_already_staged`/`rm -rf "$DEST"` guard
  # above, skipped in this mode) — every file _write_runtime_bundle_
  # manifest is about to re-scan is exactly what a prior real staging run
  # (now signed, out-of-band) left behind.
  _write_runtime_bundle_manifest
  echo "[ok] $TARGET: runtime-bundle.json refreshed in place (no download, no re-stage)"
  exit 0
fi

case "$TARGET" in
  x86_64-unknown-linux-gnu|aarch64-unknown-linux-gnu) _stage_linux ;;
  aarch64-apple-darwin)                               _stage_macos ;;
esac
_stage_app_files

# Permissions + symlinks + (on macOS) xattrs, uniformly, regardless of which
# staging path ran — see lib/normalize-staged-tree.sh for why this exists.
echo "    normalizing staged tree (permissions, symlinks$([ "$(uname -s)" = Darwin ] && echo ', xattrs'))..."
normalize_staged_tree "$DEST" || exit "$EXIT_STAGE"

_write_runtime_bundle_manifest
_record_app_files_in_lock

total_bytes="$(find "$DEST" -type f -exec stat -c '%s' {} \; 2>/dev/null | awk '{s+=$1} END {print s+0}')"
[ -n "$total_bytes" ] && [ "$total_bytes" -gt 0 ] || \
  total_bytes="$(find "$DEST" -type f -exec stat -f '%z' {} \; | awk '{s+=$1} END {print s+0}')"
cap_bytes="$(jq -r '.github_release_asset_cap_bytes' "$LOCKFILE")"
echo "[ok] $TARGET staged: $((total_bytes / 1024 / 1024)) MiB under $DEST"
echo "     (GitHub Releases per-file cap: $((cap_bytes / 1024 / 1024 / 1024)) GiB — the runtime bundle alone is $(awk -v b="$total_bytes" -v c="$cap_bytes" 'BEGIN{printf "%.1f", 100*b/c}')% of it; the rest of the budget is the Tauri shell + the app itself)"
