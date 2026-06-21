"""Query the active-state of the remote-access systemd services.

Uses `systemctl is-active` — read-only, unprivileged (any user can run it).
The tunnel unit depends on the operator's choice: named tunnel (stable URL,
requires a cloudflared token at /etc/hermes/credentials/cloudflare-tunnel.token)
or quick tunnel (ephemeral trycloudflare URL, zero credentials — the
plug-and-play default). hermes-novnc must be active in both modes.

Isolated here so the API, the D-Bus verbs, and the GTK client share the same
logic and so tests can mock it without touching subprocess.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_NAMED_TUNNEL_TOKEN = Path("/etc/hermes/credentials/cloudflare-tunnel.token")


def _remote_services() -> tuple[str, ...]:
    tunnel = (
        "hermes-remote-tunnel.service"
        if _NAMED_TUNNEL_TOKEN.exists()
        else "hermes-remote-quicktunnel.service"
    )
    return (tunnel, "hermes-novnc.service")


def all_services_active() -> bool:
    """Return True when ALL remote-access services report 'active'.

    Fail-safe: returns False on any subprocess error.
    """
    for service in _remote_services():
        if not _is_active(service):
            return False
    return True


def _is_active(service: str) -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", service],
            capture_output=True,
            check=False,
            timeout=5,
        )
        return result.stdout.decode().strip() == "active"
    except Exception:  # noqa: BLE001
        return False
