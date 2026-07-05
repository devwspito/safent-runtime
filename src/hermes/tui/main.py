"""hermes.tui.main — entry point for Safent Terminal (`hermes-tui`).

Usage:
    hermes-tui            # connect to the daemon; fall back to offline if down
    hermes-tui --offline  # force offline demo mode (no bus)
"""

from __future__ import annotations

import argparse
import logging

from hermes.tui.app import SafentTerminal
from hermes.tui.bridge import OfflineRuntimeBridge


def main() -> None:
    parser = argparse.ArgumentParser(prog="hermes-tui", description="Safent Terminal")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Forzar modo sin conexión (sin D-Bus), para demo/desarrollo.",
    )
    parser.add_argument(
        "--log",
        default="warning",
        help="Nivel de log (debug/info/warning/error).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log.upper(), logging.WARNING))

    bridge = OfflineRuntimeBridge() if args.offline else None
    SafentTerminal(bridge=bridge).run()


if __name__ == "__main__":
    main()
