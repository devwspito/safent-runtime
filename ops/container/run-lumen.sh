#!/usr/bin/env bash
# run-lumen.sh — canonical HARDENED launch for the Lumen standard container.
#
# This is the secure-by-default posture validated by the red-team (penetrate +
# escape). The desktop wrapper / OSS users should launch with THESE flags — not a
# bare `docker run`. See SECURITY.md for what each flag enforces and the host
# requirements (a Landlock-capable kernel).
#
#   ./run-lumen.sh [IMAGE] [HOST_PORT]
#
set -euo pipefail

IMAGE="${1:-ghcr.io/devwspito/lumen:latest}"
HOST_PORT="${2:-17517}"
NAME="${LUMEN_NAME:-lumen}"
RUNTIME="$(command -v podman || command -v docker)"
HERE="$(cd "$(dirname "$0")" && pwd)"
SECCOMP="${LUMEN_SECCOMP:-$HERE/seccomp/lumen.json}"

[ -n "$RUNTIME" ] || { echo "need podman or docker"; exit 1; }
[ -f "$SECCOMP" ] || { echo "seccomp profile not found: $SECCOMP"; exit 1; }

"$RUNTIME" rm -f "$NAME" >/dev/null 2>&1 || true

# WHY each flag (see SECURITY.md):
#   -p 127.0.0.1:...    publish on host LOOPBACK only — the control plane never
#                       faces the LAN. (The HTTP edge also requires a Bearer token.)
#   --cap-add NET_ADMIN add ONLY the three caps the cage needs on top of podman's
#   --cap-add SYS_ADMIN default (already-reduced) set: NET_ADMIN (veth + nftables +
#   --cap-add AUDIT_READ netns), SYS_ADMIN (create the netns + transient units),
#                       AUDIT_READ (audit). NET_ADMIN is NOT in podman's default set
#                       (only NET_BIND_SERVICE is), so it must be explicit or the
#                       netns jail fails to build. We do NOT --cap-drop ALL: systemd
#                       PID1 + journald + dbus + keygen need the default baseline to
#                       boot. Least-privilege for the AGENT is enforced PER-UNIT
#                       (CapabilityBoundingSet= empty on the browser/exec/terminal
#                       units) + non-root uid 880 — not at the container level.
#                       NEVER --privileged (that re-opens container escape).
#   --security-opt seccomp=<profile>  kernel syscall backstop: allows landlock_*
#                       (so the browser FS jail loads) + denies mount/setns/ptrace/
#                       pivot_root (so a Chromium 0-day can't escape the netns).
#   --security-opt unmask=/sys/kernel/security  let hermes-landlock-assert read the
#                       LSM list (read-only) to fail-closed if Landlock is absent.
#   -v /sys/kernel/security:ro  expose securityfs read-only for the same check.
#   --shm-size=1g       Chromium needs a real /dev/shm.
#   -v lumen-data       persist /var/lib/hermes (keystore, audit, config) across
#                       image updates (so master.key / provider keys survive pull).
# NOTE: NoNewPrivileges is set PER-UNIT (the hardened units), NOT container-wide —
# a container-level no-new-privileges breaks dbus/login setuid and the boot fails.
exec "$RUNTIME" run -d --name "$NAME" --systemd=always \
  -p "127.0.0.1:${HOST_PORT}:7517" \
  --cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add AUDIT_READ \
  --security-opt "seccomp=${SECCOMP}" \
  --security-opt unmask=/sys/kernel/security \
  -v /sys/kernel/security:/sys/kernel/security:ro \
  -v lumen-data:/var/lib/hermes \
  --shm-size=1g \
  "$IMAGE"
