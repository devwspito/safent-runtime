"""Entry point for hermes-compositor.service (systemd system unit).

Run as:
    python3 -m hermes.lumen.compositor

Responsibilities:
  1. Start the Qt/QML Wayland compositor (Compositor.qml).
  2. Wire SeatInputAdapter (QWaylandSeat) and FramebufferCaptureAdapter
     (QQuickWindow.grabWindow) into SessionInputBridge.
  3. Serve the bridge's AF_UNIX socket in-process (same event loop as Qt,
     bridged via asyncio + QEventLoop integration).
  4. Emit sd_notify READY=1 after the QML window is exposed.

The same security boundary as the desktop edition is preserved:
  - Socket /run/hermes/session-input.sock, chmod 0660.
  - SO_PEERCRED UID check against the hermes daemon UID.
  - Per-boot token at /run/hermes/session-input.token.
  - Key-chord denylist, rate-limit, and InputOwnershipLedger — all in the
    bridge, zero changes required.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pwd
import secrets
import sys
from pathlib import Path

logger = logging.getLogger("hermes-compositor")


def _configure_logging() -> None:
    try:
        from hermes.logging_setup import configure_structured_logging
        configure_structured_logging(
            service="hermes-compositor", version="0.1.0"
        )
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
    import socket as _sock
    if sock_path.startswith("@"):
        sock_path = "\0" + sock_path[1:]
    try:
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_DGRAM) as s:
            s.connect(sock_path)
            s.sendall(msg.encode())
    except OSError as exc:
        logger.warning("sd_notify failed: %s", exc)


def _write_token(token: str) -> None:
    from hermes.shell_server.session_agent.input_bridge import TOKEN_PATH
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    # CRÍTICO (cross-user): el compositor corre como hermes-user pero el DAEMON
    # (hermes) debe LEER este token para autenticarse al bridge de input. Un
    # fichero 0600 owned by hermes-user NO es legible por el daemon (0600 no da
    # lectura de grupo; da igual el dir padre — el comentario del cliente asumía
    # mal). Fix: grupo hermes + 0640 (owner rw, group r). hermes-user pertenece al
    # grupo hermes, así que puede chgrp. Mismo patrón que el chown del mcp-launcher.
    try:
        import grp as _grp  # noqa: PLC0415
        os.chown(TOKEN_PATH, -1, _grp.getgrnam("hermes").gr_gid)
    except (KeyError, PermissionError, OSError) as exc:
        logger.warning("hermes.compositor.token_chgrp_failed: %s", exc)
    TOKEN_PATH.chmod(0o640)
    logger.info("hermes.compositor.token_written path=%s (0640 group=hermes)", TOKEN_PATH)


def _write_daemon_uid(uid: int) -> None:
    from hermes.shell_server.session_agent.input_bridge import DAEMON_UID_PATH
    DAEMON_UID_PATH.write_text(str(uid), encoding="utf-8")
    DAEMON_UID_PATH.chmod(0o644)


def _daemon_uid() -> int:
    return pwd.getpwnam("hermes").pw_uid


def _run_bridge_loop(bridge) -> None:
    """Run the asyncio bridge server in a background thread."""
    import threading

    async def _serve():
        await bridge.start()
        logger.info("hermes.compositor.bridge_ready")
        await bridge.serve_forever()

    def _thread():
        asyncio.run(_serve())

    t = threading.Thread(target=_thread, daemon=True, name="bridge-loop")
    t.start()


def main() -> int:
    _configure_logging()

    # Per-boot token
    token = secrets.token_hex(32)
    _write_token(token)

    try:
        uid = _daemon_uid()
    except KeyError:
        logger.error("hermes.compositor.hermes_user_not_found")
        return 1
    _write_daemon_uid(uid)

    from PySide6.QtGui import QGuiApplication
    from PySide6.QtCore import QTimer

    from hermes.agents_os.application.teaching.input_ownership_ledger import InputOwnershipLedger
    from hermes.shell_server.session_agent.input_bridge import SessionInputBridge
    from hermes.lumen.compositor.compositor_app import build_application
    from hermes.lumen.compositor.seat_input_adapter import SeatInputAdapter
    from hermes.lumen.compositor.framebuffer_capture_adapter import FramebufferCaptureAdapter

    app, engine = build_application(sys.argv)
    if not engine.rootObjects():
        logger.error("hermes.compositor.qml_load_failed")
        return 1

    root = engine.rootObjects()[0]

    # Extract QWaylandSeat from the compositor root object.
    seat_obj = root.property("seat")
    seat_adapter = SeatInputAdapter(seat_obj)

    # Extract the compositor window for framebuffer capture.
    # TODO(H0-HARDWARE): on RK3588 confirm the window reference survives
    # after the KMS surface is acquired.
    compositor_window = root.property("window") if hasattr(root, "property") else None
    capture_adapter = FramebufferCaptureAdapter(compositor_window)

    ledger = InputOwnershipLedger()
    bridge = SessionInputBridge(
        token=token,
        ledger=ledger,
        mirror=seat_adapter,
        capture_backend=capture_adapter,
        daemon_uid=uid,
    )

    _run_bridge_loop(bridge)

    # Clipboard server INTEGRADO: el compositor posee el QClipboard (su selección
    # Wayland). wl-copy/wl-paste externos NO funcionan con QtWaylandCompositor
    # (no expone wlr-data-control) → servimos el clipboard por HTTP desde aquí,
    # mismo contrato :7519 que consume el overlay noVNC.
    try:
        from hermes.lumen.compositor.clipboard_server import start_clipboard_server
        start_clipboard_server()
    except Exception as exc:  # noqa: BLE001 — el clipboard es opcional, no tumba la sesión
        logger.warning("hermes.compositor.clipboard_server_skip: %s", exc)

    # sd_notify once the Qt event loop is running and the window is shown.
    QTimer.singleShot(200, lambda: _sd_notify("READY=1\nSTATUS=hermes-compositor ready\n"))

    logger.info("hermes.compositor.starting")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
