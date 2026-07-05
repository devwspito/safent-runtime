#!/usr/bin/env bash
# run-safent.sh — canonical HARDENED launch for the Safent standard container.
#
# This is the secure-by-default posture validated by the red-team (penetrate +
# escape). The desktop wrapper / OSS users should launch with THESE flags — not a
# bare `docker run`. See SECURITY.md for what each flag enforces and the host
# requirements (a Landlock-capable kernel).
#
#   ./run-safent.sh [IMAGE] [HOST_PORT]
#
set -euo pipefail

IMAGE="${1:-ghcr.io/devwspito/safent:latest}"
HOST_PORT="${2:-17517}"
NAME="${SAFENT_NAME:-safent}"
RUNTIME="$(command -v podman || command -v docker)"
HERE="$(cd "$(dirname "$0")" && pwd)"
SECCOMP="${SAFENT_SECCOMP:-$HERE/seccomp/safent.json}"

[ -n "$RUNTIME" ] || { echo "need podman or docker"; exit 1; }
[ -f "$SECCOMP" ] || { echo "seccomp profile not found: $SECCOMP"; exit 1; }

"$RUNTIME" rm -f "$NAME" >/dev/null 2>&1 || true

# Timezone: the container must reason/schedule in the SAME wall-clock as the host
# that runs it — otherwise it defaults to UTC and the agent tells you the wrong
# time (e.g. "it's 11 PM" when your clock says 1 AM). Resolve the host IANA zone:
#   1. an explicit SAFENT_TZ / TZ wins (override for remote/headless installs),
#   2. else read the /etc/localtime symlink (works on macOS and Linux),
#   3. else fall back to UTC.
host_tz() {
  if [ -n "${SAFENT_TZ:-}" ]; then printf '%s' "$SAFENT_TZ"; return; fi
  if [ -n "${TZ:-}" ]; then printf '%s' "$TZ"; return; fi
  local link
  link="$(readlink /etc/localtime 2>/dev/null || true)"
  case "$link" in
    */zoneinfo/*) printf '%s' "${link##*/zoneinfo/}" ;;
    *) printf 'UTC' ;;
  esac
}
SAFENT_TZ_VALUE="$(host_tz)"

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
#   --security-opt label=disable  on SELinux-enforcing hosts (Fedora/RHEL, and the
#                       Fedora CoreOS VM that backs `podman machine` on macOS) SELinux
#                       denies the container reading securityfs → the Landlock assert
#                       wrongly sees "no Landlock" and fail-closes. Disabling the SELinux
#                       label for THIS container restores the read (the cage's real
#                       confinement is Landlock/seccomp/netns/uid inside, not the outer
#                       SELinux label). No-op on AppArmor/no-LSM hosts.
#   --shm-size=1g       Chromium needs a real /dev/shm.
#   -v safent-data       persist /var/lib/hermes (keystore, audit, config) across
#                       image updates (so master.key / provider keys survive pull).
# NOTE: NoNewPrivileges is set PER-UNIT (the hardened units), NOT container-wide —
# a container-level no-new-privileges breaks dbus/login setuid and the boot fails.
exec "$RUNTIME" run -d --name "$NAME" --systemd=always \
  -p "127.0.0.1:${HOST_PORT}:7517" \
  -e "TZ=${SAFENT_TZ_VALUE}" -e "HERMES_TZ=${SAFENT_TZ_VALUE}" \
  --cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add AUDIT_READ \
  --security-opt "seccomp=${SECCOMP}" \
  --security-opt unmask=/sys/kernel/security \
  --security-opt label=disable \
  -v /sys/kernel/security:/sys/kernel/security:ro \
  -v safent-data:/var/lib/hermes \
  --shm-size=1g \
  "$IMAGE"
