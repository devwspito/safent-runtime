"""hermes bootc-updater oneshot entrypoint (FR-008, FR-009).

Driven by bootc-updater.timer. Performs the OTA "check + stage" step:
reads the current bootc status and, if a managed update channel is
configured (HERMES_UPDATE_IMAGE_REF), stages the new image (A/B) for the
next boot. Promotion and rollback decisions live in OtaOrchestrator; this
oneshot only performs the check+stage and exits.

Run:
    python3 -m hermes.bootc_updater_service check-and-stage

If no update channel is configured, the oneshot logs and exits 0 — a node
without a managed channel is a valid, non-error state, not a crash. This is
the personal-desktop default (the user updates manually via `bootc upgrade`).
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("hermes-bootc-updater")

_SUBCOMMAND = "check-and-stage"


def _check_and_stage() -> int:
    from hermes.agents_os.infrastructure.bootc_updater import (  # noqa: PLC0415
        BootcCommandError,
        BootcUpdater,
    )

    image_ref = os.environ.get("HERMES_UPDATE_IMAGE_REF", "").strip()
    updater = BootcUpdater()

    try:
        status = updater.status()
    except (BootcCommandError, FileNotFoundError) as exc:
        logger.warning(
            "hermes.bootc_updater.status_unavailable: %s — skipping OTA check", exc
        )
        return 0

    logger.info(
        "hermes.bootc_updater.status",
        extra={"booted": status.booted_digest, "staged": status.staged_digest},
    )

    if not image_ref:
        logger.info(
            "hermes.bootc_updater.no_channel — HERMES_UPDATE_IMAGE_REF unset; "
            "no managed update channel, nothing to stage"
        )
        return 0

    try:
        updater.fetch_and_stage(image_ref)
    except BootcCommandError as exc:
        logger.error("hermes.bootc_updater.stage_failed: %s", exc)
        return 1

    logger.info("hermes.bootc_updater.staged", extra={"image_ref": image_ref})
    return 0


def main() -> int:
    from hermes.logging_setup import configure_structured_logging  # noqa: PLC0415

    configure_structured_logging(service="hermes-bootc-updater", version="0.1.0")

    args = sys.argv[1:]
    subcommand = args[0] if args else _SUBCOMMAND
    if subcommand != _SUBCOMMAND:
        logger.error(
            "hermes.bootc_updater.unknown_subcommand: %r (expected %r)",
            subcommand,
            _SUBCOMMAND,
        )
        return 2
    return _check_and_stage()


if __name__ == "__main__":
    raise SystemExit(main())
