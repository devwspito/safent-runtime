"""hermes-audit-tail service entrypoint (finding #28).

Instantiates AuditTailWriter and starts the background flush thread that
ships signed audit entries to the Hermes control plane.

Run:
    python3 -m hermes.audit_tail_service [--watch /var/lib/hermes/audit]
                                         [--systemd-notify]

The service:
  1. Resolves spool_dir from --watch arg or HERMES_AUDIT_SPOOL_DIR.
  2. Builds HttpsAuditTailTransport from HERMES_CP_AUDIT_URL (or NoopTransport
     if not configured — service still runs for local spool only).
  3. Instantiates AuditTailWriter + calls start_background().
  4. Sends sd_notify READY=1.
  5. Runs a stats health loop until SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("hermes-audit-tail")

_DEFAULT_SPOOL = "/var/lib/hermes/audit-tail-pending"
# Debe ser <= WatchdogSec/2 (unit: WatchdogSec=120) o el ping llega en el deadline
# y systemd manda SIGABRT en bucle. 60s = WatchdogSec/2 con margen.
_HEALTH_INTERVAL_S = float(os.environ.get("HERMES_HEALTH_INTERVAL_S", "60"))


def _parse_args() -> tuple[Path, bool]:
    args = sys.argv[1:]
    spool_dir = Path(_DEFAULT_SPOOL)
    systemd_notify = False
    i = 0
    while i < len(args):
        if args[i] == "--watch" and i + 1 < len(args):
            spool_dir = Path(args[i + 1])
            i += 2
        elif args[i] == "--systemd-notify":
            systemd_notify = True
            i += 1
        else:
            i += 1
    return spool_dir, systemd_notify


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


def _build_writer(spool_dir: Path):
    from hermes.agents_os.infrastructure.audit_tail_writer import (  # noqa: PLC0415
        AuditTailWriter,
        FakeAuditTailTransport,
        HttpsAuditTailTransport,
    )

    audit_url = os.environ.get("HERMES_CP_AUDIT_URL")
    if audit_url:
        cert = os.environ.get("HERMES_CP_CLIENT_CERT")
        key = os.environ.get("HERMES_CP_CLIENT_KEY")
        transport = HttpsAuditTailTransport(
            url=audit_url,
            client_cert=cert,
            client_key=key,
        )
        logger.info("hermes.audit_tail.transport=https", extra={"url": audit_url})
    else:
        transport = FakeAuditTailTransport()
        logger.warning(
            "hermes.audit_tail.transport=noop — "
            "HERMES_CP_AUDIT_URL not set; entries spooled locally only"
        )
    return AuditTailWriter(transport=transport, spool_dir=spool_dir)


async def _run(*, spool_dir: Path, systemd_notify: bool) -> None:
    from hermes.logging_setup import configure_structured_logging  # noqa: PLC0415

    configure_structured_logging(service="hermes-audit-tail", version="0.1.0")

    logger.info("hermes.audit_tail.starting", extra={"spool_dir": str(spool_dir)})

    writer = _build_writer(spool_dir)
    writer.start_background()

    if systemd_notify:
        _sd_notify("READY=1\nSTATUS=hermes-audit-tail ready\n")

    _last_published = 0
    _last_failures = 0

    try:
        while True:
            stats = writer.stats()
            activity_changed = (
                stats.published_total != _last_published
                or stats.failures_total != _last_failures
            )
            if activity_changed:
                _last_published = stats.published_total
                _last_failures = stats.failures_total
                logger.info(
                    "hermes.audit_tail.health",
                    extra={
                        "queued": stats.queued_in_memory,
                        "pending": stats.persisted_pending,
                        "published": stats.published_total,
                        "failures": stats.failures_total,
                    },
                )
            else:
                # No activity since last tick — log at DEBUG to avoid I/O churn.
                logger.debug(
                    "hermes.audit_tail.health.idle",
                    extra={
                        "queued": stats.queued_in_memory,
                        "published": stats.published_total,
                    },
                )
            _sd_notify(
                f"WATCHDOG=1\nSTATUS=published={stats.published_total} "
                f"failures={stats.failures_total}\n"
            )
            await asyncio.sleep(_HEALTH_INTERVAL_S)
    finally:
        writer.stop()


def main() -> int:
    spool_dir, systemd_notify = _parse_args()
    try:
        asyncio.run(_run(spool_dir=spool_dir, systemd_notify=systemd_notify))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
