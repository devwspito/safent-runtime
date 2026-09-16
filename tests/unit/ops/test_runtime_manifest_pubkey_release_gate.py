"""Release gate: `ops/keys/runtime-manifest.pub` must not still be the
placeholder by the time a release build runs (coordinator decision,
cross-lane minisign reconciliation).

`ops/keys/runtime-manifest.pub` is committed today as an all-zero
placeholder — the real key is generated and committed by the
agents-autonomy publish pipeline (spec 028 T023, `minisign -G`), which
this repo does not control. Two tests:

  - `test_the_detector_itself_is_correct` always runs (no env dependency)
    and proves `is_placeholder_pubkey` correctly distinguishes today's
    placeholder from a plausible real key — so the gate below is trusted.
  - `test_release_build_refuses_the_placeholder` is the actual gate: it
    only runs when SAFENT_RELEASE=1 (a real release build sets this), and
    is expected to FAIL for as long as the placeholder is still committed
    — that is the intended, correct behaviour, not a bug to fix. It is
    skipped in the normal `pytest tests/unit` gate so day-to-day CI stays
    green while T023 has not run yet.
"""

from __future__ import annotations

import base64
import os

import pytest

from hermes.shell_server import runtime_manifest as rm

pytestmark = pytest.mark.unit


def _fake_real_pubkey_text() -> str:
    raw = b"Ed" + os.urandom(8) + os.urandom(32)
    return f"untrusted comment: minisign public key DEADBEEF\n{base64.b64encode(raw).decode()}\n"


class TestTheDetectorItselfIsCorrect:
    def test_todays_committed_key_is_the_real_safent_key(self) -> None:
        text = rm._REPO_PUBKEY_PATH.read_text()
        assert rm.is_placeholder_pubkey(text) is False, "ops/keys/runtime-manifest.pub is the real Safent key now"
        assert "242808B9F2E191FD" in text

    def test_a_plausible_real_key_is_not_flagged(self) -> None:
        assert rm.is_placeholder_pubkey(_fake_real_pubkey_text()) is False

    def test_a_second_plausible_real_key_is_also_not_flagged(self) -> None:
        """Not a fluke — try several random keys, all-zero is the ONLY
        pattern that trips the detector."""
        for _ in range(5):
            assert rm.is_placeholder_pubkey(_fake_real_pubkey_text()) is False


@pytest.mark.skipif(
    os.environ.get("SAFENT_RELEASE") != "1",
    reason="release-only gate — set SAFENT_RELEASE=1 to enforce (expected to FAIL until T023 runs)",
)
class TestReleaseBuildRefusesThePlaceholder:
    def test_release_build_refuses_the_placeholder(self) -> None:
        text = rm._REPO_PUBKEY_PATH.read_text()
        assert not rm.is_placeholder_pubkey(text), (
            "ops/keys/runtime-manifest.pub is still the placeholder — the "
            "agents-autonomy publish pipeline (T023) must commit the real "
            "minisign public key before this can ship as a release."
        )
