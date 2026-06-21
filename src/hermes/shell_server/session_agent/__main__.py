"""Entry point for hermes-session-input.service (systemd user unit).

Run as:
    python3 -m hermes.shell_server.session_agent

Responsibilities:
  1. Generate a per-boot random token, write to TOKEN_PATH (0600, owned by
     hermes-user). The daemon reads this file to authenticate its requests.
  2. Write daemon UID to DAEMON_UID_PATH so the bridge can do SO_PEERCRED.
  3. Start MutterMirrorSession (waits for mutter to be available).
  4. Start SessionInputBridge serving /run/hermes/session-input.sock.
  5. Emit sd_notify READY=1 when serving.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from hermes.shell_server.session_agent.input_bridge import (
    DAEMON_UID_PATH,
    TOKEN_PATH,
    SessionInputBridge,
)

logger = logging.getLogger("hermes-session-input")


def _configure_logging() -> None:
    try:
        from hermes.logging_setup import configure_structured_logging
        configure_structured_logging(service="hermes-session-input", version="0.1.0")
    except ImportError:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            stream=sys.stderr,
        )


def _sd_notify(msg: str) -> None:
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    import socket
    if sock_path.startswith("@"):
        sock_path = "\0" + sock_path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(sock_path)
            s.sendall(msg.encode())
    except OSError as exc:
        logger.warning("sd_notify failed: %s", exc)


def _wait_for_mutter(timeout_s: int = 90) -> bool:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            reply = bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "NameHasOwner",
                GLib.Variant("(s)", ("org.gnome.Mutter.RemoteDesktop",)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
            if reply.unpack()[0]:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _daemon_uid() -> int:
    """Resolve UID of the 'hermes' system user (the daemon runs as that user)."""
    import pwd
    return pwd.getpwnam("hermes").pw_uid


async def _main() -> int:
    _configure_logging()

    # Per-boot token
    token = secrets.token_hex(32)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    TOKEN_PATH.chmod(0o600)
    logger.info("hermes.session_input.token_written path=%s", TOKEN_PATH)

    # Daemon UID
    try:
        uid = _daemon_uid()
    except KeyError:
        logger.error("hermes.session_input.hermes_user_not_found")
        return 1
    DAEMON_UID_PATH.write_text(str(uid), encoding="utf-8")
    DAEMON_UID_PATH.chmod(0o644)

    if not _wait_for_mutter():
        logger.error("hermes.session_input.no_mutter")
        return 1

    from hermes.agents_os.application.teaching.input_ownership_ledger import InputOwnershipLedger
    from hermes.shell_server.mirror.mutter_mirror import MutterMirrorSession
    from hermes.shell_server.screen_capture.service import MutterGstBackend

    ledger = InputOwnershipLedger()
    mirror = MutterMirrorSession()
    try:
        mirror.start()
    except Exception as exc:
        logger.error("hermes.session_input.mirror_start_failed: %s", exc)
        return 1

    bridge = SessionInputBridge(
        token=token,
        ledger=ledger,
        mirror=mirror,
        capture_backend=MutterGstBackend(),
        daemon_uid=uid,
    )
    await bridge.start()
    _sd_notify("READY=1\nSTATUS=hermes-session-input ready\n")
    logger.info("hermes.session_input.ready")

    try:
        await bridge.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        await bridge.stop()
        mirror.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
