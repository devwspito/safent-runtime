"""hermes-whisper service entrypoint.

Starts the WhisperWorker backed by FasterWhisperBackend.

Run:
    python3 -m hermes.whisper_service [--model /path/to/model] [--systemd-notify]

The service:
  1. Resolves the model path (--model arg or HERMES_WHISPER_MODEL env var).
  2. Lazy-imports FasterWhisperBackend (only available on device with the model).
  3. Starts WhisperWorker background thread.
  4. Sends sd_notify READY=1.
  5. Runs a watchdog health loop until SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("hermes-whisper")

_DEFAULT_MODEL = "/opt/models/distil-large-v3"
_HEALTH_INTERVAL_S = float(os.environ.get("HERMES_HEALTH_INTERVAL_S", "20"))


def _parse_args() -> tuple[str, bool]:
    args = sys.argv[1:]
    model_path = _DEFAULT_MODEL
    systemd_notify = False
    i = 0
    while i < len(args):
        if args[i] == "--model" and i + 1 < len(args):
            model_path = args[i + 1]
            i += 2
        elif args[i] == "--systemd-notify":
            systemd_notify = True
            i += 1
        else:
            i += 1
    return model_path, systemd_notify


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


def _build_backend(model_path: str):
    from hermes.agents_os.infrastructure.faster_whisper_backend import (  # noqa: PLC0415
        FasterWhisperBackend,
    )

    return FasterWhisperBackend(model_path=Path(model_path))


async def _run(*, model_path: str, systemd_notify: bool) -> None:
    from hermes.logging_setup import configure_structured_logging  # noqa: PLC0415

    configure_structured_logging(service="hermes-whisper", version="0.1.0")
    from hermes.agents_os.application.whisper_worker import WhisperWorker  # noqa: PLC0415

    logger.info("hermes.whisper.starting", extra={"model_path": model_path})

    try:
        backend = _build_backend(model_path)
    except Exception as exc:
        logger.error(
            "hermes.whisper.backend_init_failed: %s — model absent or FasterWhisper "
            "not installed. Service cannot transcribe. Exiting.",
            exc,
        )
        sys.exit(1)

    worker = WhisperWorker(backend=backend)
    worker.start_background()
    logger.info("hermes.whisper.worker_started")

    if systemd_notify:
        _sd_notify("READY=1\nSTATUS=hermes-whisper ready\n")

    try:
        while True:
            depth = worker.queue_depth()
            logger.debug("hermes.whisper.health", extra={"queue_depth": depth})
            _sd_notify(f"WATCHDOG=1\nSTATUS=queue_depth={depth}\n")
            await asyncio.sleep(_HEALTH_INTERVAL_S)
    finally:
        worker.stop()


def main() -> int:
    model_path, systemd_notify = _parse_args()
    try:
        asyncio.run(_run(model_path=model_path, systemd_notify=systemd_notify))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
