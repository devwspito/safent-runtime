"""Entry point for the clipboard bridge.

Run as:
    python3 -m hermes.clipboard_bridge

Or via the systemd user service hermes-clipboard-bridge.service.

Exposure requirement
--------------------
The bridge listens on 127.0.0.1:7519.  For the noVNC overlay to reach it from
the operator's Mac/PC, port 7519 MUST be tunnelled through the same path as
noVNC (:6080).  Two supported approaches:

1. cloudflared (production OS tunnel):
   hermes-remote-tunnel.service currently tunnels only :6080.  Add a second
   cloudflared service (or use `cloudflared tunnel --url http://localhost:7519`)
   and store its URL in /var/lib/hermes/clipboard-bridge-url.  OR, if you can
   accept a single tunnel, replace the ExecStart in hermes-remote-tunnel with
   a `cloudflared tunnel --ingress` config that routes /clipboard-bridge/* to
   :7519 and /* to :6080, then the overlay JS only needs one origin.

   Simplest single-tunnel option (no extra config):
   Update hermes-remote-tunnel.service to use an ingress rule config file, e.g.:
     tunnel: <uuid>
     ingress:
       - hostname: <tunnel-host>
         path: /cb/.*
         service: http://localhost:7519
       - service: http://localhost:6080

2. QEMU dev (hostfwd):
   Add to the QEMU launch command:
     -netdev user,...,hostfwd=tcp::7519-:7519
   Then the overlay calls http://localhost:7519/clipboard from the Mac.

3. Tailscale (alternative to cloudflared):
     tailscale funnel --bg --https=443 --set-path=/clipboard-bridge http://localhost:7519

The clipboard-overlay.js reads the bridge base URL from the query parameter
`clipboard_bridge` in the noVNC URL, or falls back to
`${window.location.protocol}//${window.location.hostname}:7519`.
"""

from __future__ import annotations

import logging
import os
import sys

from hermes.clipboard_bridge.server import build_server
from hermes.clipboard_bridge.wayland_backend import WaylandClipboardBackend

_PORT = int(os.environ.get("HERMES_CLIPBOARD_BRIDGE_PORT", "7519"))
_HOST = os.environ.get("HERMES_CLIPBOARD_BRIDGE_HOST", "127.0.0.1")


def _configure_logging() -> None:
    try:
        from hermes.logging_setup import configure_structured_logging  # noqa: PLC0415

        configure_structured_logging(service="hermes-clipboard-bridge", version="0.4.0")
    except ImportError:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            stream=sys.stderr,
        )


def main() -> int:
    _configure_logging()
    logger = logging.getLogger("hermes-clipboard-bridge")

    backend = WaylandClipboardBackend()

    # Smoke-test the backend once at startup.  Log a warning but do NOT crash —
    # the Wayland session may take a few seconds to expose WAYLAND_DISPLAY after
    # the service unit starts.  Individual request handlers fail-soft per call.
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
    if not wayland_display:
        logger.warning(
            "hermes.clipboard_bridge.no_wayland_display",
            extra={
                "hint": "WAYLAND_DISPLAY not set — wl-copy/wl-paste will fail until the session is ready"
            },
        )

    server = build_server(host=_HOST, port=_PORT, backend=backend)
    logger.info(
        "hermes.clipboard_bridge.listening",
        extra={"host": _HOST, "port": _PORT},
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("hermes.clipboard_bridge.shutdown")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
