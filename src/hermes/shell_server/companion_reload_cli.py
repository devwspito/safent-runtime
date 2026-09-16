"""companion_reload_cli — host-triggered hot-reload of a companion's MCP
presence, no daemon restart (028 T017).

Usage (inside the container, invoked by `safent companion install|repair`
on the HOST, right after a successful provisioning run):
  python3 -m hermes.shell_server.companion_reload_cli reload <slug>

Why this exists
----------------
`ReloadCompanionPresence` (contracts/sso.md-adjacent, T017) is allow-listed
on `org.hermes.Runtime1.conf` for the shell-server's own uid (`hermes`)
ONLY — the SAME boundary `MintCompanionOwnerAssertion` uses (the call
carries no human identity to authorize against, and it is a system
reconciliation step, not an operator action). The host-side `safent` CLI
has no Python/D-Bus client of its own; this thin wrapper is what `podman
exec -u hermes $NAME python3 -m hermes.shell_server.companion_reload_cli
reload <slug>` actually runs, reusing the exact D-Bus call shape
`brake_release_cli.py` already established for a host-only, no-REST-bearer
verb invocation — never a second implementation of the D-Bus client plumbing.

`safent companion install|repair` requires the daemon's authenticated health
result through `install_request_agent_cli verify-ads`. The daemon attempts
MCP reconnection before checking health; this caller must allow that bounded
cold-start work without treating a slow reconnect as an unreachable API.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

_WELL_KNOWN_NAME = "org.hermes.Runtime"
_OBJECT_PATH = "/org/hermes/Runtime"
_INTERFACE_NAME = "org.hermes.Runtime1"
# One overall ceiling, including bus connect/introspection. The actual Ads
# npx factory allows 120s for initialize (runtime.__main__); launcher connect
# plus send/receive allow 5+30+30s, close allows 15s, and authenticated health
# allows 8s: 208s plus 32s for dispatch/other RPC work. This remains a hard
# deadline, not a guarantee of readiness or an unlimited wait for list_tools.
# The host CLI emits a heartbeat every 5s throughout this call.
_DBUS_CALL_TIMEOUT_S = 240.0


async def _reload_companion_presence(slug: str) -> dict:
    """Call ReloadCompanionPresence(slug) on the system bus. Raises on any
    D-Bus/transport error — the caller turns that into a clear, non-zero-exit
    CLI failure."""
    import json  # noqa: PLC0415

    from dbus_fast import BusType  # noqa: PLC0415
    from dbus_fast.aio import MessageBus  # noqa: PLC0415

    bus = MessageBus(bus_type=BusType.SYSTEM)
    try:
        async with asyncio.timeout(_DBUS_CALL_TIMEOUT_S):
            await bus.connect()
            introspection = await bus.introspect(_WELL_KNOWN_NAME, _OBJECT_PATH)
            proxy = bus.get_proxy_object(_WELL_KNOWN_NAME, _OBJECT_PATH, introspection)
            iface = proxy.get_interface(_INTERFACE_NAME)
            raw = await iface.call_reload_companion_presence(slug)
            return dict(json.loads(raw))
    finally:
        if getattr(bus, "connected", False):
            bus.disconnect()


def cmd_reload(slug: str) -> int:
    try:
        result = asyncio.run(_reload_companion_presence(slug))
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller, never swallowed
        print(f"[x] Could not reload the companion's presence: {exc}", file=sys.stderr)
        return 1
    if not result.get("ok"):
        print(f"[x] Reload reported failure: {result}", file=sys.stderr)
        return 1
    print(f"[ok] Companion '{slug}' presence reloaded: {result}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="companion_reload_cli")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    reload_parser = sub.add_parser("reload", help="Reload a companion's MCP presence.")
    reload_parser.add_argument("slug")
    args = parser.parse_args(argv)

    if args.subcommand == "reload":
        return cmd_reload(args.slug)
    parser.print_usage(sys.stderr)  # pragma: no cover — argparse already exits on bad subcommand
    return 1


if __name__ == "__main__":
    sys.exit(main())
