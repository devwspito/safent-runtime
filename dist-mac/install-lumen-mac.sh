#!/usr/bin/env bash
# install-lumen-mac.sh — install + run Lumen (the Playwright container) on macOS.
#
# Lumen ships as a Docker/OCI CONTAINER (systemd PID1 + the kernel cage). On macOS
# we run it inside a podman machine (a small Linux VM) — podman supports the
# systemd + caps + seccomp the cage needs (Docker Desktop's systemd story is fiddly).
#
#   1) Installs podman (via Homebrew) if missing + starts a podman machine.
#   2) BUILDS the Lumen image from this repo (self-contained; no registry/login).
#      Override with LUMEN_IMAGE=<ref> to pull a prebuilt image instead.
#   3) Runs it with the correct flags (NO --cap-drop ALL — that breaks systemd PID1).
#   4) Prints the ready-to-open URL WITH the bootstrap token (?k=...).
#
# Usage:   ./dist-mac/install-lumen-mac.sh
set -euo pipefail

IMAGE_REF="${LUMEN_IMAGE:-ghcr.io/devwspito/lumen-runtime:clean}"
LOCAL_TAG="lumen-runtime:clean"
NAME="${LUMEN_NAME:-lumen}"
PORT="${LUMEN_PORT:-17517}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
SECCOMP="${HERE}/ops/container/seccomp/lumen.json"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── 0. sanity ────────────────────────────────────────────────────────────────
[ "$(uname -s)" = "Darwin" ] || die "This installer is for macOS."
ARCH="$(uname -m)"
if [ "$ARCH" != "arm64" ]; then
  echo "WARNING: this Mac is $ARCH; the published image is arm64 (Apple Silicon)."
  echo "         On Intel, set LUMEN_IMAGE to an amd64 build before running."
fi
[ -f "$SECCOMP" ] || die "seccomp profile not found at $SECCOMP — run from a clone of lumen-runtime."

# ── 1. podman + machine ──────────────────────────────────────────────────────
if ! command -v podman >/dev/null 2>&1; then
  command -v brew >/dev/null 2>&1 || die "Homebrew not found. Install it from https://brew.sh then re-run."
  say "Installing podman via Homebrew…"
  brew install podman
fi

if ! podman machine inspect lumen-machine >/dev/null 2>&1; then
  say "Creating podman machine (4 CPU / 8GB / 30GB, ROOTFUL — the cage needs root in the VM)…"
  podman machine init lumen-machine --rootful --cpus 4 --memory 8192 --disk-size 30
fi
# The cage (cgroup memory.pressure mount, netns, nftables, securityfs) needs ROOT in the
# VM. A rootless machine fails hermes-runtime.service with 226/NAMESPACE ("Permission
# denied" mounting cgroup memory.pressure). Force rootful.
if ! podman machine inspect lumen-machine --format '{{.Rootful}}' 2>/dev/null | grep -qi true; then
  say "Switching the podman machine to ROOTFUL (required by the cage)…"
  podman machine stop lumen-machine 2>/dev/null || true
  podman machine set --rootful lumen-machine
fi
if ! podman machine inspect lumen-machine --format '{{.State}}' 2>/dev/null | grep -qi running; then
  say "Starting podman machine…"
  podman machine start lumen-machine
fi
# Make the ROOTFUL connection the default (set --rootful already does this; be explicit
# so we never run the container rootless → 226/NAMESPACE).
podman system connection default lumen-machine-root 2>/dev/null \
  || podman system connection default lumen-machine 2>/dev/null || true

# ── 2. image: BUILD from source (default, self-contained) OR pull (if LUMEN_IMAGE) ─
# ALWAYS build (do not skip on "image exists") — the fixes are BAKED into the image, so a
# stale image must be replaced. podman reuses cached layers, so an unchanged tree rebuilds
# in seconds; only changed layers (and everything after) re-run.
if [ -n "${LUMEN_IMAGE:-}" ]; then
  say "Pulling prebuilt image: $LUMEN_IMAGE"
  echo "   (auth: podman login ghcr.io -u <your-github-user>  then re-run)"
  podman pull "$LUMEN_IMAGE"
  podman tag "$LUMEN_IMAGE" "$LOCAL_TAG"
else
  # The wheel is built INSIDE the container (Containerfile), so no host python is needed —
  # this avoids the old macOS system python (3.9) mis-building the wheel as UNKNOWN-0.0.0.
  say "Building Lumen from source (podman reuses cached layers — fast if nothing changed; full ~15-20 min on a cold cache)…"
  ( cd "$HERE" && podman build -f ops/container/Containerfile -t "$LOCAL_TAG" . ) \
    || die "build failed — see the output above."
fi

# ── 2b. assert the macOS cage fixes actually got baked (catches a stale tree / bad cache) ─
if ! podman run --rm --entrypoint /bin/sh "$LOCAL_TAG" -c 'test -f /etc/systemd/system.conf.d/10-no-mempressure.conf'; then
  die "the built image is MISSING the macOS fixes (memory.pressure drop-in). Your checkout is stale — run:  git pull  (and confirm 'git log -1' shows the rootful/memory.pressure/nf_log_syslog commits), then re-run this script."
fi
say "✓ macOS cage fixes baked into the image (memory.pressure off + nf_log_syslog optional)."

# ── 3. run (canonical flags) ─────────────────────────────────────────────────
say "Starting Lumen…"
podman rm -f "$NAME" >/dev/null 2>&1 || true
podman run -d --name "$NAME" --systemd=always \
  -p "127.0.0.1:${PORT}:7517" \
  --cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add AUDIT_READ \
  --security-opt "seccomp=${SECCOMP}" \
  --security-opt unmask=/sys/kernel/security \
  -v /sys/kernel/security:/sys/kernel/security:ro \
  -v lumen-data:/var/lib/hermes \
  --shm-size=1g \
  "$LOCAL_TAG"

# ── 4. wait for boot + print the ready URL with the bootstrap token ───────────
say "Waiting for the Lumen daemon to come up…"
for _ in $(seq 1 40); do
  s="$(podman exec "$NAME" systemctl is-active hermes-runtime 2>/dev/null || true)"
  [ "$s" = "active" ] && break
  sleep 5
done
[ "${s:-}" = "active" ] || die "daemon did not become active — check: podman logs $NAME ; podman exec $NAME systemctl --failed"

SECRET="$(podman exec "$NAME" cat /var/lib/hermes-bootstrap/bootstrap/webui-bootstrap 2>/dev/null || true)"
echo ""
say "Lumen is running. Open this URL in your browser (it carries the one-time auth token):"
if [ -n "$SECRET" ]; then
  printf '\n    \033[1;32mhttp://localhost:%s/?k=%s\033[0m\n\n' "$PORT" "$SECRET"
else
  printf '\n    http://localhost:%s/   (token unavailable; mutations may 401)\n\n' "$PORT"
fi
echo "Manage:  podman logs -f $NAME   |   stop: podman stop $NAME   |   start: podman start $NAME"
