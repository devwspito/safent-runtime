"""Tests for TsaExternalAnchor, CompositeExternalAnchor, and related helpers.

Coverage:
- _verify_tst: message imprint match, imprint mismatch (tampered hash).
- TsaExternalAnchor: anchor persists token, returns ref; anchor fails → pending;
  verify True for valid TST; verify False for unknown hash; verify False for
  wrong hash (tampered chain); retry queue drains on next successful anchor.
- CompositeExternalAnchor: anchor calls both layers; verify True only if both agree.
- Unit tests use FakeTsaTransport — NO real network required.
- requires_network test: real round-trip against freeTSA.org (opt-in only).

Fixtures:
  fixture_tst_bytes  — real TST from freeTSA.org (captured 2026-05-31)
  freetsa_ca_cert    — bundled ops/audit/freetsa_tsa.crt
  fixture_hash_hex   — 'aa' * 32 (the hash anchored in the fixture TST)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_HASH_HEX = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_WRONG_HASH_HEX = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

_FIXTURE_BIN = Path(__file__).parent / "fixtures" / "fixture_tst.bin"
_FIXTURE_TSR_BIN = Path(__file__).parent / "fixtures" / "fixture_tsr.bin"
_CA_CERT_PATH = (
    Path(__file__).parent.parent.parent
    / "ops"
    / "audit"
    / "freetsa_tsa.crt"
)


@pytest.fixture()
def fixture_tst_bytes() -> bytes:
    """Real TST from freeTSA.org (captured 2026-05-31, no network needed)."""
    return _FIXTURE_BIN.read_bytes()


@pytest.fixture()
def fixture_tsr_bytes() -> bytes:
    """Real TSR from freeTSA.org (captured 2026-05-31)."""
    return _FIXTURE_TSR_BIN.read_bytes()


@pytest.fixture()
def freetsa_ca_cert() -> bytes:
    """Bundled freeTSA CA cert PEM."""
    return _CA_CERT_PATH.read_bytes()


# ---------------------------------------------------------------------------
# FakeTsaTransport
# ---------------------------------------------------------------------------


class FakeTsaTransport:
    """Injectable TsaTransport for unit tests.

    Returns pre-configured TSR bytes on success, or raises TsaNetworkError.
    Tracks call count for assertion in tests.
    """

    def __init__(
        self,
        *,
        response_bytes: bytes | None = None,
        fail: bool = False,
    ) -> None:
        self._response = response_bytes
        self._fail = fail
        self.call_count = 0

    async def post_timestamp_query(
        self, *, url: str, body: bytes, timeout_s: float  # noqa: ARG002
    ) -> bytes:
        self.call_count += 1
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaNetworkError,
        )

        if self._fail:
            raise TsaNetworkError("Fake network error")
        if self._response is None:
            raise TsaNetworkError("No response configured")
        return self._response


# ---------------------------------------------------------------------------
# _verify_tst unit tests (pure, no I/O)
# ---------------------------------------------------------------------------


class TestVerifyTst:
    def test_verify_correct_hash(
        self, fixture_tst_bytes: bytes, freetsa_ca_cert: bytes
    ) -> None:
        """TST with correct hash → True."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import _verify_tst

        result = _verify_tst(
            tst_bytes=fixture_tst_bytes,
            expected_digest_hex=FIXTURE_HASH_HEX,
            ca_cert_pem=freetsa_ca_cert,
        )
        assert result is True

    def test_verify_wrong_hash_returns_false(
        self, fixture_tst_bytes: bytes, freetsa_ca_cert: bytes
    ) -> None:
        """TST with wrong expected hash → False (tampered chain detection)."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import _verify_tst

        result = _verify_tst(
            tst_bytes=fixture_tst_bytes,
            expected_digest_hex=_WRONG_HASH_HEX,
            ca_cert_pem=freetsa_ca_cert,
        )
        assert result is False

    def test_verify_truncated_tst_returns_false(self, freetsa_ca_cert: bytes) -> None:
        """Garbage TST bytes → False (no exception leaks)."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import _verify_tst

        result = _verify_tst(
            tst_bytes=b"\x30\x01\xff",
            expected_digest_hex=FIXTURE_HASH_HEX,
            ca_cert_pem=freetsa_ca_cert,
        )
        assert result is False


# ---------------------------------------------------------------------------
# _extract_tst_bytes unit tests
# ---------------------------------------------------------------------------


class TestExtractTstBytes:
    def test_extracts_tst_from_tsr(self, fixture_tsr_bytes: bytes) -> None:
        """_extract_tst_bytes returns non-empty bytes from a valid TSR."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            _decode_timestamp_response_from_bytes,
            _extract_tst_bytes,
        )

        tsr = _decode_timestamp_response_from_bytes(fixture_tsr_bytes)
        tst_bytes = _extract_tst_bytes(tsr)
        assert isinstance(tst_bytes, bytes)
        assert len(tst_bytes) > 100  # noqa: PLR2004

    def test_extract_raises_on_rejected_status(self) -> None:
        """_extract_tst_bytes raises TsaProtocolError if status != granted."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaProtocolError,
            _extract_tst_bytes,
        )

        class _StatusValue:
            """Mimics the PKIStatusInfo.status component (int-convertible)."""

            def __int__(self) -> int:
                return 2  # rejection

            def getComponentByName(self, name: str) -> _StatusValue:  # noqa: ARG002, N802
                return self

        class _FakePkiStatusInfo:
            def getComponentByName(self, name: str) -> _StatusValue:  # noqa: ARG002, N802
                return _StatusValue()

        class _FakeTsr:
            status = _FakePkiStatusInfo()
            time_stamp_token = None

        with pytest.raises(TsaProtocolError, match="TSA rejected"):
            _extract_tst_bytes(_FakeTsr())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TsaExternalAnchor unit tests (FakeTsaTransport — no network)
# ---------------------------------------------------------------------------


class TestTsaExternalAnchorAnchor:
    @pytest.mark.asyncio
    async def test_anchor_persists_tst_file(
        self,
        tmp_path: Path,
        fixture_tsr_bytes: bytes,
        freetsa_ca_cert: bytes,
    ) -> None:
        """anchor() persists a .tsr file under token_dir keyed by hash."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        transport = FakeTsaTransport(response_bytes=fixture_tsr_bytes)
        anchor = TsaExternalAnchor(
            token_dir=tmp_path / "tokens",
            ca_cert_pem=freetsa_ca_cert,
            transport=transport,
        )

        ref = await anchor.anchor(FIXTURE_HASH_HEX)

        tst_file = tmp_path / "tokens" / f"{FIXTURE_HASH_HEX}.tsr"
        assert tst_file.exists()
        assert tst_file.stat().st_mode & 0o777 == 0o600
        assert ref.startswith("tsa:")
        assert transport.call_count == 1

    @pytest.mark.asyncio
    async def test_anchor_returns_pending_on_network_error(
        self,
        tmp_path: Path,
        freetsa_ca_cert: bytes,
    ) -> None:
        """anchor() returns pending ref when transport raises TsaNetworkError."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        transport = FakeTsaTransport(fail=True)
        anchor = TsaExternalAnchor(
            token_dir=tmp_path / "tokens",
            ca_cert_pem=freetsa_ca_cert,
            transport=transport,
        )

        ref = await anchor.anchor(FIXTURE_HASH_HEX)

        assert ref.startswith("pending:")
        tst_file = tmp_path / "tokens" / f"{FIXTURE_HASH_HEX}.tsr"
        assert not tst_file.exists()

    @pytest.mark.asyncio
    async def test_anchor_idempotent_with_existing_tst(
        self,
        tmp_path: Path,
        fixture_tst_bytes: bytes,
        fixture_tsr_bytes: bytes,
        freetsa_ca_cert: bytes,
    ) -> None:
        """anchor() skips POST if .tsr already exists (idempotent)."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        token_dir = tmp_path / "tokens"
        token_dir.mkdir()
        tst_file = token_dir / f"{FIXTURE_HASH_HEX}.tsr"
        tst_file.write_bytes(fixture_tst_bytes)

        transport = FakeTsaTransport(response_bytes=fixture_tsr_bytes)
        anchor = TsaExternalAnchor(
            token_dir=token_dir,
            ca_cert_pem=freetsa_ca_cert,
            transport=transport,
        )

        ref = await anchor.anchor(FIXTURE_HASH_HEX)

        assert ref.startswith("tsa:")
        assert transport.call_count == 0  # no POST when already anchored


class TestTsaExternalAnchorVerify:
    @pytest.mark.asyncio
    async def test_verify_true_for_anchored_head(
        self,
        tmp_path: Path,
        fixture_tst_bytes: bytes,
        freetsa_ca_cert: bytes,
    ) -> None:
        """verify() returns True when TST is present and valid."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        token_dir = tmp_path / "tokens"
        token_dir.mkdir()
        tst_file = token_dir / f"{FIXTURE_HASH_HEX}.tsr"
        tst_file.write_bytes(fixture_tst_bytes)

        anchor = TsaExternalAnchor(
            token_dir=token_dir,
            ca_cert_pem=freetsa_ca_cert,
            transport=FakeTsaTransport(fail=True),
        )

        result = await anchor.verify(FIXTURE_HASH_HEX)
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_false_for_unknown_hash(
        self,
        tmp_path: Path,
        freetsa_ca_cert: bytes,
    ) -> None:
        """verify() returns False when no TST file exists (fail-closed)."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        anchor = TsaExternalAnchor(
            token_dir=tmp_path / "tokens",
            ca_cert_pem=freetsa_ca_cert,
            transport=FakeTsaTransport(fail=True),
        )

        result = await anchor.verify(_WRONG_HASH_HEX)
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_false_for_tampered_chain(
        self,
        tmp_path: Path,
        fixture_tst_bytes: bytes,
        freetsa_ca_cert: bytes,
    ) -> None:
        """Tampered chain: TST anchored for hash_A, verify(hash_B) → False.

        This is the core detection: root rewrites the chain and replaces the
        head_hash. The TST for the original head exists but the tampered head
        has no TST → verify returns False (AUD-2 detection).
        """
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        token_dir = tmp_path / "tokens"
        token_dir.mkdir()
        # Persist TST for FIXTURE_HASH, but verify with wrong hash.
        tst_file = token_dir / f"{FIXTURE_HASH_HEX}.tsr"
        tst_file.write_bytes(fixture_tst_bytes)

        anchor = TsaExternalAnchor(
            token_dir=token_dir,
            ca_cert_pem=freetsa_ca_cert,
            transport=FakeTsaTransport(fail=True),
        )

        # _WRONG_HASH_HEX has no TST → fail-closed
        result = await anchor.verify(_WRONG_HASH_HEX)
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_false_for_imprint_mismatch(
        self,
        tmp_path: Path,
        fixture_tst_bytes: bytes,
        freetsa_ca_cert: bytes,
    ) -> None:
        """TST file present but keyed to wrong hash (content mismatch) → False."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        token_dir = tmp_path / "tokens"
        token_dir.mkdir()
        # Store fixture TST (imprint=FIXTURE_HASH) under the WRONG hash key.
        tst_file = token_dir / f"{_WRONG_HASH_HEX}.tsr"
        tst_file.write_bytes(fixture_tst_bytes)

        anchor = TsaExternalAnchor(
            token_dir=token_dir,
            ca_cert_pem=freetsa_ca_cert,
            transport=FakeTsaTransport(fail=True),
        )

        result = await anchor.verify(_WRONG_HASH_HEX)
        assert result is False


class TestTsaExternalAnchorQueue:
    @pytest.mark.asyncio
    async def test_pending_queue_persisted_on_failure(
        self,
        tmp_path: Path,
        freetsa_ca_cert: bytes,
    ) -> None:
        """Failed anchor adds hash to pending_queue.json."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        token_dir = tmp_path / "tokens"
        anchor = TsaExternalAnchor(
            token_dir=token_dir,
            ca_cert_pem=freetsa_ca_cert,
            transport=FakeTsaTransport(fail=True),
        )

        await anchor.anchor(FIXTURE_HASH_HEX)

        queue_file = token_dir / "pending_queue.json"
        assert queue_file.exists()
        queue = json.loads(queue_file.read_text())
        assert FIXTURE_HASH_HEX in queue

    @pytest.mark.asyncio
    async def test_pending_queue_drained_on_success(
        self,
        tmp_path: Path,
        fixture_tsr_bytes: bytes,
        freetsa_ca_cert: bytes,
    ) -> None:
        """Pending queue is cleared after a successful anchor call."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        token_dir = tmp_path / "tokens"
        failing_transport = FakeTsaTransport(fail=True)
        anchor = TsaExternalAnchor(
            token_dir=token_dir,
            ca_cert_pem=freetsa_ca_cert,
            transport=failing_transport,
        )

        # First attempt fails → enqueued.
        await anchor.anchor(FIXTURE_HASH_HEX)
        queue = json.loads((token_dir / "pending_queue.json").read_text())
        assert FIXTURE_HASH_HEX in queue

        # Second attempt succeeds → queue drained.
        anchor._transport = FakeTsaTransport(response_bytes=fixture_tsr_bytes)
        a_different_hash = _WRONG_HASH_HEX  # trigger flush of the queue
        await anchor.anchor(a_different_hash)

        queue = json.loads((token_dir / "pending_queue.json").read_text())
        assert FIXTURE_HASH_HEX not in queue

    @pytest.mark.asyncio
    async def test_get_latest_returns_none_when_empty(
        self, tmp_path: Path, freetsa_ca_cert: bytes
    ) -> None:
        """get_latest() returns None if nothing anchored yet."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        anchor = TsaExternalAnchor(
            token_dir=tmp_path / "tokens",
            ca_cert_pem=freetsa_ca_cert,
            transport=FakeTsaTransport(fail=True),
        )

        result = await anchor.get_latest()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_latest_returns_last_anchored(
        self,
        tmp_path: Path,
        fixture_tsr_bytes: bytes,
        freetsa_ca_cert: bytes,
    ) -> None:
        """get_latest() returns the last successfully anchored hash."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            TsaExternalAnchor,
        )

        token_dir = tmp_path / "tokens"
        anchor = TsaExternalAnchor(
            token_dir=token_dir,
            ca_cert_pem=freetsa_ca_cert,
            transport=FakeTsaTransport(response_bytes=fixture_tsr_bytes),
        )

        await anchor.anchor(FIXTURE_HASH_HEX)
        latest = await anchor.get_latest()
        assert latest == FIXTURE_HASH_HEX


# ---------------------------------------------------------------------------
# CompositeExternalAnchor unit tests
# ---------------------------------------------------------------------------


class TestCompositeExternalAnchor:
    @pytest.mark.asyncio
    async def test_anchor_calls_both_layers(
        self,
        tmp_path: Path,
        fixture_tsr_bytes: bytes,
        freetsa_ca_cert: bytes,
    ) -> None:
        """anchor() on composite produces worm file + TST file."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            CompositeExternalAnchor,
            TsaExternalAnchor,
            WormFileAnchor,
        )

        worm_path = tmp_path / "anchor.log"
        token_dir = tmp_path / "tokens"
        worm = WormFileAnchor(anchor_path=worm_path)
        tsa = TsaExternalAnchor(
            token_dir=token_dir,
            ca_cert_pem=freetsa_ca_cert,
            transport=FakeTsaTransport(response_bytes=fixture_tsr_bytes),
        )
        composite = CompositeExternalAnchor(worm=worm, tsa=tsa)

        ref = await composite.anchor(FIXTURE_HASH_HEX)

        assert worm_path.exists()
        assert FIXTURE_HASH_HEX in worm_path.read_text()
        assert (token_dir / f"{FIXTURE_HASH_HEX}.tsr").exists()
        assert "worm:" in ref

    @pytest.mark.asyncio
    async def test_verify_true_only_when_both_agree(
        self,
        tmp_path: Path,
        fixture_tst_bytes: bytes,
        freetsa_ca_cert: bytes,
    ) -> None:
        """verify() → True only if worm and TSA both confirm."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            CompositeExternalAnchor,
            TsaExternalAnchor,
            WormFileAnchor,
        )

        worm_path = tmp_path / "anchor.log"
        token_dir = tmp_path / "tokens"
        token_dir.mkdir()

        worm = WormFileAnchor(anchor_path=worm_path)
        tsa = TsaExternalAnchor(
            token_dir=token_dir,
            ca_cert_pem=freetsa_ca_cert,
            transport=FakeTsaTransport(fail=True),
        )
        composite = CompositeExternalAnchor(worm=worm, tsa=tsa)

        # Pre-anchor worm manually.
        await worm.anchor(FIXTURE_HASH_HEX)
        # Pre-place TST for TSA layer.
        (token_dir / f"{FIXTURE_HASH_HEX}.tsr").write_bytes(fixture_tst_bytes)

        assert await composite.verify(FIXTURE_HASH_HEX) is True

    @pytest.mark.asyncio
    async def test_verify_false_if_worm_missing(
        self,
        tmp_path: Path,
        fixture_tst_bytes: bytes,
        freetsa_ca_cert: bytes,
    ) -> None:
        """verify() → False if worm has no entry (even if TSA has TST)."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            CompositeExternalAnchor,
            TsaExternalAnchor,
            WormFileAnchor,
        )

        worm_path = tmp_path / "anchor.log"
        token_dir = tmp_path / "tokens"
        token_dir.mkdir()

        worm = WormFileAnchor(anchor_path=worm_path)
        tsa = TsaExternalAnchor(
            token_dir=token_dir,
            ca_cert_pem=freetsa_ca_cert,
            transport=FakeTsaTransport(fail=True),
        )
        composite = CompositeExternalAnchor(worm=worm, tsa=tsa)

        # Only TSA layer has the TST; worm is empty.
        (token_dir / f"{FIXTURE_HASH_HEX}.tsr").write_bytes(fixture_tst_bytes)

        assert await composite.verify(FIXTURE_HASH_HEX) is False

    @pytest.mark.asyncio
    async def test_verify_false_if_tsa_missing(
        self,
        tmp_path: Path,
        freetsa_ca_cert: bytes,
    ) -> None:
        """verify() → False if TSA has no TST (even if worm has entry)."""
        from hermes.capabilities.infrastructure.tsa_external_anchor import (
            CompositeExternalAnchor,
            TsaExternalAnchor,
            WormFileAnchor,
        )

        worm_path = tmp_path / "anchor.log"
        worm = WormFileAnchor(anchor_path=worm_path)
        await worm.anchor(FIXTURE_HASH_HEX)

        tsa = TsaExternalAnchor(
            token_dir=tmp_path / "tokens",
            ca_cert_pem=freetsa_ca_cert,
            transport=FakeTsaTransport(fail=True),
        )
        composite = CompositeExternalAnchor(worm=worm, tsa=tsa)

        assert await composite.verify(FIXTURE_HASH_HEX) is False


# ---------------------------------------------------------------------------
# Requires-network: real round-trip against freeTSA.org (opt-in, CI excluded)
# ---------------------------------------------------------------------------


@pytest.mark.requires_network
@pytest.mark.asyncio
async def test_real_round_trip_freetsa(tmp_path: Path) -> None:
    """Full RFC-3161 round-trip: anchor() → verify() against real freeTSA.org.

    This test contacts the network. Run with:
        pytest -m requires_network tests/capabilities/test_tsa_external_anchor.py

    Asserts:
    - anchor() succeeds and returns a tsa: ref.
    - .tsr file is persisted under token_dir.
    - verify() returns True using the bundled cert chain.
    - get_latest() returns the anchored hash.
    """
    import hashlib

    from hermes.capabilities.infrastructure.tsa_external_anchor import TsaExternalAnchor

    test_hash_hex = hashlib.sha256(b"hermes_tsa_integration_2026").hexdigest()
    token_dir = tmp_path / "tokens"

    anchor = TsaExternalAnchor(token_dir=token_dir)

    ref = await anchor.anchor(test_hash_hex)
    assert ref.startswith("tsa:"), f"Expected tsa: ref, got: {ref}"

    tst_file = token_dir / f"{test_hash_hex}.tsr"
    assert tst_file.exists(), "TST file not persisted"
    assert tst_file.stat().st_size > 100  # noqa: PLR2004

    verified = await anchor.verify(test_hash_hex)
    assert verified is True, "verify() should be True after successful anchor"

    latest = await anchor.get_latest()
    assert latest == test_hash_hex
