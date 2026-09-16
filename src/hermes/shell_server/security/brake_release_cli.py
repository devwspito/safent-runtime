"""brake_release_cli — sovereign, host-only fallback to release the emergency
brake without MFA (R17, spec 025 matriz item R17).

Usage (inside the container, invoked by `safent brake release` on the HOST):
  python3 -m hermes.shell_server.security.brake_release_cli release

Why this exists
----------------
`POST /api/v1/security/kill-switch {"engaged": false}` (security_api.py) demands
either the owner's TOTP (MFA enrolled) or their device password, verified via
the PAM root-helper `hermes-tailscale-control` (MFA not enrolled). On a FRESH
install neither exists yet: no TOTP secret, and no device/OS password was ever
set for the account PAM checks against — the gate fails closed (403
invalid_device_password) with NO way to release a brake that can be engaged
before the owner ever enrolls MFA (spec 025, hallazgo C / R17). The only
surviving path was enrolling TOTP with the brake still engaged — awkward, and
only possible at all because enrollment itself does not check the brake.

The sovereign fallback: whoever can run `podman exec` on THIS container already
IS the owner (host access is the trust boundary this whole product is built
on — same posture as `safent pair`/`safent companion rotate`/every other host
verb that reaches into the container without an HTTP bearer). So this script
calls `org.hermes.Runtime1.Resume()` on the SYSTEM D-Bus bus DIRECTLY, the
EXACT SAME authorization path the native GTK compositor shell already uses
for every D-Bus call it makes (operator_token.py: "Local operator calls (GTK
shell -> D-Bus direct, uid = hermes-user) continue unchanged — they are
already correct") — no REST layer, no MFA gate, no operator_token to mint.

`safent brake release` execs this AS `hermes-user` (uid 1000, `podman exec -u
hermes-user`) — the SAME uid `_resolve_operator_uid()` resolves to and the
SAME uid `HERMES_OPERATOR_ID` encodes (UUID(int=1000)). The daemon's
`_authorize_and_resolve` authorizes that uid DIRECTLY (it is in
`authorized_uids`) and attributes the resulting audit entry
(`AGENT_RESUMED`, signed hash-chain, `changed_by`) to the REAL owner
identity — identical provenance to a release from the desktop shell or the
UI. Never run this exec as root or as `hermes` (uid 880, the daemon/
shell-server's own service uid): neither is the resolved operator uid, and
attributing a release to the service's own identity instead of the owner's
would be a worse audit trail, not a better one.

TOTP stays the PRIMARY path (security_api.py, unchanged) — this is reached
only from the host CLI, never from the network-facing REST/UI surface.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from hermes.tasks.domain.ports import AgentPauseProvenance

_WELL_KNOWN_NAME = "org.hermes.Runtime"
_OBJECT_PATH = "/org/hermes/Runtime"
_INTERFACE_NAME = "org.hermes.Runtime1"
_DBUS_CALL_TIMEOUT_S = 8.0

# Security review 2026-09-10 (MEDIUM finding, CWE-778/STRIDE-R): this exact
# string is threaded through Resume() -> request_resume ->
# AgentStatePort.resume -> the signed AGENT_RESUMED audit entry, so an
# incident review can tell "released via host CLI, no MFA" apart from a
# TOTP/device-password release (security_api.py's own "totp"/
# "device_password" reasons). Matched by
# tests/unit/shell_server/test_brake_release_cli.py. Reuses the shared
# AgentPauseProvenance enum (025 re-verificación d2eb8c6) — same closed
# vocabulary AGENT_PAUSED now also draws from, so pause and resume are never
# fed two independently-drifting sets of provenance strings.
_RELEASE_REASON = AgentPauseProvenance.HOST_CLI


async def _release_brake() -> bool:
    """Call Resume(reason="host_cli") on the system bus. Raises on any D-Bus/
    transport error — the caller turns that into a clear, non-zero-exit CLI
    failure (fail-closed: an owner who sees this command succeed must be
    able to trust it worked)."""
    from dbus_fast import BusType  # noqa: PLC0415
    from dbus_fast.aio import MessageBus  # noqa: PLC0415

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect(_WELL_KNOWN_NAME, _OBJECT_PATH)
        proxy = bus.get_proxy_object(_WELL_KNOWN_NAME, _OBJECT_PATH, introspection)
        iface = proxy.get_interface(_INTERFACE_NAME)
        return bool(
            await asyncio.wait_for(
                iface.call_resume(_RELEASE_REASON), timeout=_DBUS_CALL_TIMEOUT_S
            )
        )
    finally:
        if getattr(bus, "connected", False):
            bus.disconnect()


def cmd_release() -> int:
    try:
        released = asyncio.run(_release_brake())
    except Exception as exc:  # noqa: BLE001 — surfaced to the owner, never swallowed
        print(f"[x] Could not release the brake: {exc}", file=sys.stderr)
        return 1
    if not released:
        print("[x] Resume() returned false — the brake may still be engaged.", file=sys.stderr)
        return 1
    print(
        "[ok] Emergency brake released (sovereign host CLI, audited as the "
        f'owner, reason="{_RELEASE_REASON}").'
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brake_release_cli")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    sub.add_parser("release", help="Release the emergency brake without MFA.")
    args = parser.parse_args(argv)

    if args.subcommand == "release":
        return cmd_release()
    parser.print_usage(sys.stderr)  # pragma: no cover — argparse already exits on bad subcommand
    return 1


if __name__ == "__main__":
    sys.exit(main())
