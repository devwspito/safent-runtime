#!/usr/bin/env bash
# run-safent.sh — canonical HARDENED launch for the Safent standard container.
#
# This is the secure-by-default posture validated by the red-team (penetrate +
# escape). The desktop wrapper / OSS users should launch with THESE flags — not a
# bare `docker run`. See SECURITY.md for what each flag enforces and the host
# requirements (a Landlock-capable kernel).
#
#   ./run-safent.sh [IMAGE] [HOST_PORT] [--codex-auth <path-to-auth.json>] [--no-companion]
#   ./run-safent.sh --help
#
# --no-companion: OPTIONAL. Skip provisioning/joining the safent-ads companion
#   entirely (FR-6) — Safent starts with no --network/binds for it. Use this
#   to keep the pre-024 self-hosted-URL path (Herramientas -> Safent Ads).
#
# Graceful stop: the container is started with --stop-signal=SIGRTMIN+3 (PID1
#   is systemd; SIGTERM alone never triggers an orderly shutdown of its units)
#   and --stop-timeout (default 30s, override with SAFENT_STOP_TIMEOUT_S) —
#   both apply automatically to a bare `podman stop`/`restart` on this
#   container, no extra flags needed at stop time.
#
# Backup/restore: not this script's job — use the `safent` CLI's own
#   `safent backup [dir]` / `safent restore <archive> [--force]`, which stop
#   this container cleanly, archive/restore the data volume + companion state
#   + seccomp cache, and restart it.
#
# --codex-auth <path>: OPTIONAL. Bind-mounts an EXISTING, host-side OpenAI
#   Codex CLI auth.json (from a `codex login` the owner already did on the
#   HOST) read-only into the container at $HOME/.codex/auth.json (HOME is
#   fixed to /var/lib/hermes/hermes-home by hermes-runtime.service) and
#   points CODEX_HOME at the same directory. hermes-agent 0.21.1 has TWO
#   independent consumers of that one file, both covered by this single flag:
#     1. hermes_cli/auth_codex.py::_import_codex_cli_tokens /
#        _recover_codex_tokens_from_cli — Hermes's OWN Codex OAuth session
#        (~/.hermes/auth.json, a separate store) silently self-heals from
#        this file when its refresh_token is rejected (rotation conflict),
#        instead of surfacing a hard 401. This benefits BOTH normal OpenAI
#        Codex / ChatGPT provider paths below transparently — no extra
#        container flag needed for them either way.
#     2. The OPT-IN `codex_app_server` runtime (agent/transports/
#        codex_app_server.py) — spawns the REAL `codex` binary, which reads
#        CODEX_HOME/auth.json itself. Same file, same env var.
#   Both normal OpenAI Codex / ChatGPT provider paths (Safent's own UI,
#   Settings -> Providers -> OpenAI Codex / ChatGPT (suscripción)) still need
#   no container flag on their own:
#     1. ChatGPT subscription, device-code login (the default: click "Iniciar
#        sesión con ChatGPT" — dbus_runtime_service.py's _codex_oauth_worker).
#     2. Your own OpenAI API key, pay-per-token fallback ("Usar clave de API
#        en su lugar" on the same card — plan.md D-A4).
#
# Safent Ads (MCP campaign tools, Google/Meta) is PREINSTALLED as a companion
# (024): this script always scaffolds it (network + CA + bearer +
# companions.json — see ops/container/companions/ads/provision.sh
# --scaffold, 028 T015) BEFORE starting Safent, then joins Safent to the
# fixed `safent-companions` network and binds the four read-only files
# under /etc/hermes/companions.json — no URL to paste. Scaffolding is local
# and image-independent (no pull, no compose up): the companion's actual
# SERVICE only comes up on an explicit `safent companion install|repair`
# (T016) — Safent's own container is never recreated for that, because the
# bind-mount sources already exist from this scaffold step. If scaffolding
# itself fails (subnet/port already taken — never re-chosen, see
# provision.sh), Safent still starts, just without the companion (FR-3); the
# owner can fall back to a self-hosted MCP URL via Herramientas -> "Safent
# Ads" -> Conectar (hermes.shell_server.managed_remote_endpoints), or skip
# provisioning entirely with --no-companion.
set -euo pipefail

IMAGE="ghcr.io/devwspito/safent:latest"
HOST_PORT="17517"
CODEX_AUTH_PATH=""
NO_COMPANION=0
_positional_index=0

usage() {
  # Print the leading comment block (everything up to the first non-#
  # line after the shebang) instead of a hardcoded line range — a fixed
  # range silently truncates usage() mid-sentence every time a note is
  # added to the header (as happened here: item #2's stop-signal/timeout
  # note pushed the block past the old '2,45p').
  awk 'NR==1{next} /^#/{print; next} {exit}' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --codex-auth)
      [ $# -ge 2 ] || { echo "--codex-auth requires a path"; exit 1; }
      CODEX_AUTH_PATH="$2"
      shift 2
      ;;
    --codex-auth=*)
      CODEX_AUTH_PATH="${1#*=}"
      shift
      ;;
    --no-companion)
      NO_COMPANION=1
      shift
      ;;
    *)
      case "$_positional_index" in
        0) IMAGE="$1" ;;
        1) HOST_PORT="$1" ;;
        *) echo "unexpected argument: $1"; exit 1 ;;
      esac
      _positional_index=$((_positional_index + 1))
      shift
      ;;
  esac
done

NAME="${SAFENT_NAME:-safent}"
# Volume follows the container name so a test container (SAFENT_NAME=next-smoke)
# can never mount production's safent-data by accident. Override with SAFENT_VOLUME.
VOLUME="${SAFENT_VOLUME:-${NAME}-data}"
# SAFENT_PODMAN wins over PATH resolution — same rule as the `safent` CLI
# (contracts/app-engine.md §1): the desktop app ships its own pinned podman.
RUNTIME="${SAFENT_PODMAN:-$(command -v podman || command -v docker)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SECCOMP="${SAFENT_SECCOMP:-$HERE/seccomp/safent.json}"

[ -n "$RUNTIME" ] || { echo "need podman or docker"; exit 1; }
[ -f "$SECCOMP" ] || { echo "seccomp profile not found: $SECCOMP"; exit 1; }

# CODEX_HOME lives inside the ALREADY-mounted safent-data volume (HERMES_HOME
# is /var/lib/hermes/hermes-home — see ops/agents-os-edition/systemd/hermes-
# runtime.service) so it persists across image updates like every other
# credential. Read-only: the container never writes back to the host's file.
CODEX_AUTH_MOUNT=()
if [ -n "$CODEX_AUTH_PATH" ]; then
  [ -f "$CODEX_AUTH_PATH" ] || { echo "codex auth file not found: $CODEX_AUTH_PATH"; exit 1; }
  CODEX_AUTH_MOUNT=(
    -v "${CODEX_AUTH_PATH}:/var/lib/hermes/hermes-home/.codex/auth.json:ro"
    -e "CODEX_HOME=/var/lib/hermes/hermes-home/.codex"
  )
fi

# Companion (024) — provision BEFORE the container starts (companions.json
# has to exist for the read-only bind below). A provisioning failure is
# NEVER fatal to Safent's own boot (FR-3): we just skip --network/the binds
# and Safent starts companion-less, exactly like --no-companion.
COMPANION_STATE="${SAFENT_COMPANION_STATE:-$HOME/.safent/companions/ads}"
COMPANION_RUN_ARGS=()
if [ "$NO_COMPANION" -eq 0 ]; then
  # Dev convenience, simplest rule: if SAFENT_ADS_IMAGE is unset AND a
  # locally-built safent-ads:local already exists on this host (the
  # historical dev workflow — build the ads image by hand, no publishing
  # from a developer machine involved), use it; otherwise leave it unset so
  # provision.sh falls back to its own default, the published
  # ghcr.io/devwspito/safent-ads:latest release.
  if [ -z "${SAFENT_ADS_IMAGE:-}" ] && "$RUNTIME" image inspect safent-ads:local >/dev/null 2>&1; then
    export SAFENT_ADS_IMAGE=safent-ads:local
  fi
  if "$HERE/companions/ads/provision.sh" --scaffold; then
    COMPANION_RUN_ARGS=(
      --network safent-companions
      -v "safent-companion-runtime:/etc/hermes/companions:ro"
    )
  else
    echo "run-safent.sh: companion provisioning failed — starting Safent WITHOUT it (FR-3)" >&2
  fi
fi

# AppArmor (Linux hosts only — Ubuntu/Debian ship it enforcing by default).
# On a ROOTFUL podman run under an enforcing AppArmor, podman applies its
# `containers-default-*` profile, and that profile denies the mount(2)/
# proc-write set systemd needs as PID1 with CAP_SYS_ADMIN: PID1 dies before
# it writes a single log line and the container exits 255 immediately. The
# cage's real confinement is Landlock + seccomp + the netns jail + uid 880
# INSIDE the container (same argument as --security-opt label=disable above
# for SELinux) — the outer AppArmor profile adds nothing we rely on and
# costs us the boot. Added ONLY when AppArmor is actually enabled: on macOS
# (and inside the Fedora CoreOS VM that backs `podman machine`) this file
# does not exist, the array stays empty, and that path is untouched.
APPARMOR_RUN_ARGS=()
if [ "$(cat /sys/module/apparmor/parameters/enabled 2>/dev/null || true)" = "Y" ]; then
  APPARMOR_RUN_ARGS=(--security-opt apparmor=unconfined)
fi

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
#   -v ${NAME}-data      persist /var/lib/hermes (keystore, audit, config) across
#                       image updates (so master.key / provider keys survive pull).
#   --stop-signal/--stop-timeout  PID1 is systemd, which needs SIGRTMIN+3 (not
#                       SIGTERM) to begin an orderly shutdown of every unit.
#                       STOPSIGNAL SIGRTMIN+3 is baked into the image (Containerfile),
#                       but pinning it here too keeps `podman stop`/`restart` correct
#                       even against an older/custom image that predates it. 30s is
#                       generous margin over a verified clean shutdown (~6s); a
#                       bare `podman stop $NAME` (no explicit -t) falls back to this
#                       container-level default. Same value as the `safent` CLI's
#                       own STOP_TIMEOUT_S — keep the two in sync.
# NOTE: NoNewPrivileges is set PER-UNIT (the hardened units), NOT container-wide —
# a container-level no-new-privileges breaks dbus/login setuid and the boot fails.
STOP_TIMEOUT_S="${SAFENT_STOP_TIMEOUT_S:-30}"
exec "$RUNTIME" run -d --name "$NAME" --systemd=always \
  -p "127.0.0.1:${HOST_PORT}:7517" \
  --stop-signal=SIGRTMIN+3 --stop-timeout="${STOP_TIMEOUT_S}" \
  -e "TZ=${SAFENT_TZ_VALUE}" -e "HERMES_TZ=${SAFENT_TZ_VALUE}" \
  --cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add AUDIT_READ \
  --security-opt "seccomp=${SECCOMP}" \
  --security-opt unmask=/sys/kernel/security \
  --security-opt label=disable \
  ${APPARMOR_RUN_ARGS[@]+"${APPARMOR_RUN_ARGS[@]}"} \
  -v /sys/kernel/security:/sys/kernel/security:ro \
  -v "${VOLUME}:/var/lib/hermes" \
  --shm-size=1g \
  ${CODEX_AUTH_MOUNT[@]+"${CODEX_AUTH_MOUNT[@]}"} \
  ${COMPANION_RUN_ARGS[@]+"${COMPANION_RUN_ARGS[@]}"} \
  "$IMAGE"
