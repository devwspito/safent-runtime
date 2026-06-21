"""hermes-consent-manager service entrypoint (findings #3, #18, #30).

Runs the persistent ConsentManager backed by SQLite. Exposes a minimal
health probe and integrates with systemd Type=notify.

Run:
    python3 -m hermes.consent_manager_service [--systemd-notify]

The service:
  1. Opens SQLite consent DB (HERMES_CONSENT_DB or /var/lib/hermes/consent.db).
  2. Instantiates ConsentManager with SQLiteConsentRepository (persistent).
  3. Sends sd_notify READY=1.
  4. Runs a health loop logging active consent count until SIGTERM.

Note on scope: this service owns the consent state store and is the
canonical source-of-truth for active consents. The hermes-runtime and
shell-server both mount the same SQLite DB and read/write consent via
SQLiteConsentRepository (no IPC needed for the single-node case).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("hermes-consent-manager")

_CONSENT_DB_PATH = Path(
    os.environ.get("HERMES_CONSENT_DB", "/var/lib/hermes/consent.db")
)
# Debe ser <= WatchdogSec/2 (unit: WatchdogSec=30) o el ping llega en el deadline
# y systemd manda SIGABRT en bucle. 15s = WatchdogSec/2 con margen.
_HEALTH_INTERVAL_S = float(os.environ.get("HERMES_HEALTH_INTERVAL_S", "15"))


def _sd_notify(message: str) -> None:
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return
    import socket  # noqa: PLC0415

    if notify_socket.startswith("@"):
        notify_socket = "\0" + notify_socket[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(notify_socket)
            sock.sendall(message.encode())
    except OSError as exc:
        logger.warning("sd_notify failed: %s", exc)


def _build_consent_manager():
    from hermes.agents_os.application.consent_manager import ConsentManager  # noqa: PLC0415
    from hermes.agents_os.infrastructure.sqlite_consent_repo import (  # noqa: PLC0415
        SQLiteConsentRepository,
    )

    _CONSENT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    repo = SQLiteConsentRepository(db_path=_CONSENT_DB_PATH)
    return ConsentManager(repo=repo)


async def _run(*, systemd_notify: bool) -> None:
    from hermes.logging_setup import configure_structured_logging  # noqa: PLC0415

    configure_structured_logging(service="hermes-consent-manager", version="0.1.0")

    logger.info(
        "hermes.consent_manager.starting",
        extra={"db_path": str(_CONSENT_DB_PATH)},
    )

    manager = _build_consent_manager()
    logger.info("hermes.consent_manager.ready")

    if systemd_notify:
        _sd_notify("READY=1\nSTATUS=hermes-consent-manager ready\n")

    try:
        while True:
            _sd_notify("WATCHDOG=1\nSTATUS=running\n")
            logger.debug("hermes.consent_manager.health")
            await asyncio.sleep(_HEALTH_INTERVAL_S)
    except Exception as exc:  # noqa: BLE001
        logger.error("hermes.consent_manager.fatal: %s", exc)
        raise


def main() -> int:
    args = sys.argv[1:]
    systemd_notify = "--systemd-notify" in args
    try:
        asyncio.run(_run(systemd_notify=systemd_notify))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
