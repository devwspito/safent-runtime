#!/usr/bin/env bash
# test-patch-containers-conf.sh — proves patch_containers_conf_for_bundled_helpers
# (lib/patch-containers-conf.sh) turns the upstream podman-static
# containers.conf into one that points the bundled podman at its OWN bundled
# netavark/aardvark-dns/rootlessport instead of silently falling back to
# whatever happens to already be installed on the host (confirmed live on
# the DGX: without this, netavark 1.4.0 from the system was used instead of
# the bundled 2.1.0 — see runtime-manifest.lock's containers_conf_patch note).
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TESTS_DIR/.." && pwd)"

# shellcheck source=../lib/patch-containers-conf.sh
source "$SCRIPTS_DIR/lib/patch-containers-conf.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM

fail() {
  echo "[x] FAIL: $1" >&2
  exit 1
}

CONF="$WORK/containers.conf"
# The exact upstream shape (podman-static v6.1.1's own containers.conf).
cat >"$CONF" <<'EOF'
# See https://github.com/podman-container-tools/container-libs/blob/main/common/pkg/config/containers.conf
[engine]
cgroup_manager = "cgroupfs"
events_logger="file"
EOF

patch_containers_conf_for_bundled_helpers "$CONF"

echo "[*] helper_binaries_dir was inserted into the EXISTING [engine] section (not a duplicate one)"
engine_count="$(grep -c '^\[engine\]$' "$CONF")"
[ "$engine_count" -eq 1 ] || fail "expected exactly one [engine] section, found $engine_count"
# shellcheck disable=SC2016 # literal $BINDIR token being searched for, not expanded
grep -qF 'helper_binaries_dir = ["$BINDIR/../libexec/podman", "$BINDIR"]' "$CONF" \
  || fail "helper_binaries_dir line missing or wrong"

echo "[*] the original keys are still there, untouched"
grep -qF 'cgroup_manager = "cgroupfs"' "$CONF" || fail "cgroup_manager was lost"
grep -qF 'events_logger="file"' "$CONF" || fail "events_logger was lost"

echo "[*] a new [network] section sets default_rootless_network_cmd = pasta"
grep -qF '[network]' "$CONF" || fail "[network] section missing"
grep -qF 'default_rootless_network_cmd = "pasta"' "$CONF" || fail "default_rootless_network_cmd missing"

echo "[*] the result is valid enough TOML that the SAME section header never appears twice"
[ "$(grep -c '^\[network\]$' "$CONF")" -eq 1 ] || fail "duplicate [network] section"

echo "[*] the transformation is deterministic — same input, same output, byte for byte"
CONF2="$WORK/containers2.conf"
cp "$CONF" "$WORK/containers.conf.bak"
cat >"$CONF2" <<'EOF'
# See https://github.com/podman-container-tools/container-libs/blob/main/common/pkg/config/containers.conf
[engine]
cgroup_manager = "cgroupfs"
events_logger="file"
EOF
patch_containers_conf_for_bundled_helpers "$CONF2"
diff "$WORK/containers.conf.bak" "$CONF2" >/dev/null || fail "patching the same input twice produced different output"

echo "[*] refuses a file with no [engine] section instead of silently no-op'ing"
NOENGINE="$WORK/no-engine.conf"
echo '[other]' >"$NOENGINE"
if patch_containers_conf_for_bundled_helpers "$NOENGINE" 2>/dev/null; then
  fail "should have failed on a containers.conf with no [engine] section"
fi

echo "[ok] all patch_containers_conf_for_bundled_helpers assertions passed"

# ---- MAC3-07: write_macos_containers_conf (no upstream file to patch) -----
MACOS_CONF="$WORK/macos-containers.conf"
write_macos_containers_conf "$MACOS_CONF"

echo "[*] macOS containers.conf sets helper_binaries_dir to \$BINDIR alone (flat bin/, no libexec/ split)"
[ -f "$MACOS_CONF" ] || fail "write_macos_containers_conf did not create $MACOS_CONF"
grep -qF '[engine]' "$MACOS_CONF" || fail "macOS containers.conf missing [engine] section"
# shellcheck disable=SC2016 # literal $BINDIR token being searched for, not expanded
grep -qF 'helper_binaries_dir = ["$BINDIR"]' "$MACOS_CONF" \
  || fail "macOS containers.conf: helper_binaries_dir line missing or wrong"
# Linux's own libexec/ split entry must NEVER appear here — macOS stages
# gvproxy/vfkit/krunkit flat alongside podman, not under a libexec/ subdir.
if grep -qF 'libexec' "$MACOS_CONF"; then
  fail "macOS containers.conf must not reference libexec/ (Linux-only layout)"
fi

echo "[*] write_macos_containers_conf is deterministic — same call, same output, byte for byte"
MACOS_CONF2="$WORK/macos-containers2.conf"
write_macos_containers_conf "$MACOS_CONF2"
diff "$MACOS_CONF" "$MACOS_CONF2" >/dev/null || fail "write_macos_containers_conf produced different output on a second call"

echo "[ok] all write_macos_containers_conf assertions passed"
