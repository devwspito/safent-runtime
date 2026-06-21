"""Tests BootcUpdater (FR-008, FR-009).

Sin invocar `bootc` real — solo parsing del JSON y dry_run paths.
"""

from __future__ import annotations

import pytest

from hermes.agents_os.infrastructure.bootc_updater import (
    BootcUpdater,
)

pytestmark = pytest.mark.unit


_SAMPLE_STATUS = {
    "status": {
        "booted": {
            "image": {
                "image": "quay.io/hermes/agents-os-personal-desktop:v1.0.5",
                "imageDigest": (
                    "sha256:"
                    + "a" * 64
                ),
            },
            "ostree": {"commit": "v1.0.5"},
        },
        "staged": {
            "image": {
                "image": "quay.io/hermes/agents-os-personal-desktop:v1.0.6",
                "imageDigest": (
                    "sha256:"
                    + "b" * 64
                ),
            },
            "ostree": {"commit": "v1.0.6"},
        },
        "rollback": {
            "image": {
                "image": "quay.io/hermes/agents-os-personal-desktop:v1.0.4",
                "imageDigest": (
                    "sha256:"
                    + "c" * 64
                ),
            },
            "ostree": {"commit": "v1.0.4"},
        },
    }
}


class TestParseStatus:
    def test_basic_parse(self) -> None:
        upd = BootcUpdater(dry_run=True)
        status = upd.parse_status(_SAMPLE_STATUS)
        assert status.booted_version == "v1.0.5"
        assert status.staged_version == "v1.0.6"
        assert status.rollback_image and "v1.0.4" in status.rollback_image
        assert status.booted_digest is not None
        assert status.booted_digest.startswith("sha256:")

    def test_empty_status(self) -> None:
        upd = BootcUpdater(dry_run=True)
        status = upd.parse_status({"status": {}})
        assert status.booted_image is None
        assert status.staged_image is None
        assert status.rollback_image is None

    def test_only_booted(self) -> None:
        upd = BootcUpdater(dry_run=True)
        status = upd.parse_status(
            {
                "status": {
                    "booted": {
                        "image": {
                            "image": "quay.io/hermes/agents-os-server:v0.4.0",
                            "imageDigest": "sha256:" + "d" * 64,
                        },
                        "ostree": {"commit": "v0.4.0"},
                    }
                }
            }
        )
        assert status.booted_version == "v0.4.0"
        assert status.staged_image is None


class TestDryRun:
    def test_fetch_and_stage_dry_run_noop(self) -> None:
        upd = BootcUpdater(dry_run=True)
        upd.fetch_and_stage("quay.io/hermes/agents-os-server:v1.0.6")  # no raise

    def test_rollback_dry_run_noop(self) -> None:
        BootcUpdater(dry_run=True).rollback()  # no raise

    def test_switch_dry_run_noop(self) -> None:
        BootcUpdater(dry_run=True).switch_to(
            "quay.io/hermes/agents-os-server:v1.0.6"
        )

    def test_reboot_dry_run_noop(self) -> None:
        BootcUpdater(dry_run=True).reboot_to_staged()

    def test_status_dry_run_returns_empty(self) -> None:
        status = BootcUpdater(dry_run=True).status()
        assert status.booted_image is None
        assert status.captured_at is not None
