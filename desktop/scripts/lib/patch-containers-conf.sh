#!/usr/bin/env bash
# patch-containers-conf.sh — two composable transformations stage-runtime.sh
# applies to the upstream podman-static containers.conf, both merged from
# independent lanes finding independent, real, complementary bugs against
# the SAME file. Sourced, never executed directly — kept separate from
# stage-runtime.sh so each transformation is testable on its own
# (tests/test-patch-containers-conf.sh).
set -uo pipefail

# Confirmed on this DGX (2026-09-10, see runtime-manifest.lock's
# containers_conf_patch note and research.md for the full investigation):
# WITHOUT helper_binaries_dir, a bundled podman on a host that happens to
# already have podman installed uses THAT copy's netavark instead of the
# bundled one (observed: fell back to system netavark 1.4.0 instead of the
# bundled 2.1.0). This is exactly the "depends on what's already on the
# machine" failure the whole point of bundling exists to avoid.
patch_containers_conf_for_bundled_helpers() {
  local conf="$1"
  [ -f "$conf" ] || { echo "[x] patch_containers_conf_for_bundled_helpers: not a file: $conf" >&2; return 1; }
  grep -q '^\[engine\]$' "$conf" || { echo "[x] patch_containers_conf_for_bundled_helpers: no [engine] section in $conf" >&2; return 1; }

  # $BINDIR is containers-common's OWN literal token (pkg/config/config.go,
  # FindHelperBinary) for "directory of the currently running podman binary",
  # resolved fresh at runtime via os.Executable() — NOT a shell variable, and
  # deliberately never bash-expanded here (single-quoted sed program).
  # shellcheck disable=SC2016
  sed -i '/^\[engine\]$/a helper_binaries_dir = ["$BINDIR/../libexec/podman", "$BINDIR"]' "$conf"

  cat >>"$conf" <<'EOF'

[network]
default_rootless_network_cmd = "pasta"
EOF
}

# Reproduced live on this same DGX by another lane (specs/028-safent-app-nativa/
# verificacion-paquete-linux.md, "Pasada 1"): "failed to open 2048 locks in
# /libpod_rootless_lock_1000: numerical result out of range" — a bundled
# static/musl podman and the host's own glibc podman/docker, run as the same
# uid, collide on ONE shared /dev/shm rootless-lock segment that each libc's
# pthread_mutex_t layout sizes differently. `lock_type = "file"` switches to
# per-storage-tree file locks instead of a segment shared by uid alone — no
# collision possible, regardless of what else is installed on the host.
# Idempotent: a second call on an already-patched file is a no-op.
patch_containers_conf_for_isolated_locks() {
  local conf="$1"
  [ -f "$conf" ] || { echo "[x] patch_containers_conf_for_isolated_locks: not a file: $conf" >&2; return 1; }
  grep -q '^\[engine\]$' "$conf" || { echo "[x] patch_containers_conf_for_isolated_locks: no [engine] section in $conf" >&2; return 1; }
  grep -q '^lock_type' "$conf" && return 0

  sed -i '/^\[engine\]$/a lock_type = "file"' "$conf"
}

# MAC3-07 (verificacion-mac-3.md, MAC-07/MAC2-13 repeated unfixed): unlike
# Linux (an UPSTREAM containers.conf, extracted from the podman-static
# tarball, patched above), the official macOS .pkg ships no containers.conf
# at all — this WRITES one fresh. Without it, safent's own CONTAINERS_CONF-
# from-bundle logic has nothing to find, podman resolves gvproxy/vfkit via
# PATH, and a Mac that already has podman.io's own installer in place
# silently runs ITS gvproxy/vfkit instead of the bundled, hash-verified
# ones (confirmed live: different sha256) — on a Mac with NO podman
# installed at all, machine start fails outright. ONE helper_binaries_dir
# entry, not two like Linux's: podman/gvproxy/vfkit/krunkit all land FLAT
# in the SAME bin/ on macOS (no libexec/ split). `$BINDIR` is
# containers-common's OWN literal token (resolved at runtime to "directory
# of the currently running podman binary") — the single-quoted heredoc
# delimiter keeps it unexpanded here, exactly like the Linux patch's sed
# program above.
write_macos_containers_conf() {
  local dest="$1"
  cat >"$dest" <<'EOF'
[engine]
helper_binaries_dir = ["$BINDIR"]
EOF
}
