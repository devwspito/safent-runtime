"""Tests for the config_sync loop logic.

Tests inject a fake fetch function and a fake store to exercise the sync
loop without network or D-Bus.

P0-1: Signatures are always over the full envelope (signing_bytes), not just payload.
P0-2: Signature is verified BEFORE tenant_id/freshness checks.
P1-1: Bundles outside the freshness window are rejected.
P2:   Invalid pubkey_hex (wrong length) aborts before any network call.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes.config_sync.applier import ApplyResult
from hermes.config_sync.policy_document import PolicyBundle, PolicyPayload, signing_bytes
from hermes.config_sync.__main__ import _policy_url, _sync_once
from hermes.instance.association_store import InstanceAssociation

pytestmark = pytest.mark.unit


def test_policy_url_carries_applied_version_heartbeat() -> None:
    """Each poll reports the currently-applied version so Fleet shows real
    convergence (published vs applied), not just what was published."""
    url = _policy_url("https://cloud.test/safent-control/", "inst-1", 7)
    assert url == "https://cloud.test/safent-control/v1/policy?instance_id=inst-1&applied_version=7"
    # Default (not yet applied anything) reports 0 — backward-compatible.
    assert _policy_url("https://cloud.test", "inst-1").endswith("&applied_version=0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    return key, key.public_key().public_bytes_raw().hex()


def _minimal_payload() -> PolicyPayload:
    return PolicyPayload.model_validate(
        {
            "agents": [],
            "providers": [],
            "integrations": [],
            "mcp": [],
            "skills": [],
            "egress": {"allow_domains": []},
            "consents": [],
            "features": {"views": []},
            "license": {"plan": "starter", "max_agents": 5, "expires_at": "", "views": []},
        }
    )


def _sign_bundle(
    private_key: Ed25519PrivateKey,
    payload: PolicyPayload,
    *,
    version: int,
    tenant_id: str,
    issued_at: str = "2026-06-26T10:00:00Z",
) -> PolicyBundle:
    """Sign the FULL envelope (P0-1) and return a PolicyBundle."""
    envelope = signing_bytes(
        version=version,
        tenant_id=tenant_id,
        issued_at=issued_at,
        payload=payload,
    )
    sig_hex = private_key.sign(envelope).hex()
    return PolicyBundle.model_validate(
        {
            "version": version,
            "tenant_id": tenant_id,
            "issued_at": issued_at,
            "signature_hex": sig_hex,
            "payload": payload.model_dump(),
        }
    )


def _utc_iso(delta: timedelta) -> str:
    """Return ISO-8601 UTC string offset from now by delta."""
    ts = datetime.now(tz=UTC) + delta
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeStore:
    """In-memory stub for SQLiteAssociationStore."""

    def __init__(
        self,
        *,
        is_associated: bool = True,
        last_applied_version: int = 0,
        pubkey_hex: str = "a" * 64,
        tenant_id: str = "tenant-1",
        cloud_endpoint: str = "https://cloud.safent.run",
    ) -> None:
        self._is_associated = is_associated
        self.last_applied_version = last_applied_version
        self.pubkey_hex = pubkey_hex
        self.tenant_id = tenant_id
        self.cloud_endpoint = cloud_endpoint
        self.version_updates: list[int] = []
        self.license_updates: list[dict] = []

    def is_associated(self) -> bool:
        return self._is_associated

    def get(self) -> InstanceAssociation | None:
        if not self._is_associated:
            return None
        return InstanceAssociation(
            instance_id="inst-1",
            tenant_id=self.tenant_id,
            paired_at="2026-06-26T10:00:00Z",
            cloud_endpoint=self.cloud_endpoint,
            signing_pubkey_hex=self.pubkey_hex,
            license={},
            last_applied_version=self.last_applied_version,
            state="active",
        )

    def reveal_instance_secret(self) -> str:
        return "sk-test-secret"

    def set_last_applied_version(self, version: int) -> None:
        self.version_updates.append(version)
        self.last_applied_version = version

    def update_license(self, data: dict) -> None:
        self.license_updates.append(data)


class FakeProxy:
    """No-op proxy; apply is mocked at the PolicyApplier level."""

    async def call_list(self, member: str, *args: Any) -> list[dict]:
        return []

    async def call_mutator(self, member: str, *args: Any) -> dict:
        return {"ok": True}

    async def call_bool(self, member: str, *args: Any) -> bool:
        return True

    async def call_dict(self, member: str, *args: Any) -> dict:
        return {}


# ---------------------------------------------------------------------------
# Patch helper
# ---------------------------------------------------------------------------


def _patch_apply(return_value: ApplyResult):
    """Context manager that replaces PolicyApplier.apply with a stub."""
    from hermes.config_sync.applier import PolicyApplier

    async def _stub(self, payload, **kwargs):  # noqa: ANN001
        return return_value

    return patch.object(PolicyApplier, "apply", new=_stub)


# ---------------------------------------------------------------------------
# Anti-rollback
# ---------------------------------------------------------------------------


class TestAntiRollback:
    @pytest.mark.asyncio
    async def test_same_version_skips_apply(self) -> None:
        private_key, pubkey_hex = _generate_keypair()
        payload = _minimal_payload()
        bundle = _sign_bundle(private_key, payload, version=5, tenant_id="tenant-1")
        store = FakeStore(last_applied_version=5, pubkey_hex=pubkey_hex)

        with (
            patch("hermes.config_sync.__main__._fetch_bundle", return_value=bundle),
            _patch_apply(ApplyResult(applied=0)) as mock_apply,
        ):
            from hermes.config_sync.applier import PolicyApplier
            apply_called = False
            orig = PolicyApplier.apply

            async def track_apply(self, *a, **kw):  # noqa: ANN001
                nonlocal apply_called
                apply_called = True
                return ApplyResult()

            PolicyApplier.apply = track_apply
            try:
                await _sync_once(store=store, proxy=FakeProxy())
            finally:
                PolicyApplier.apply = orig

        assert not apply_called
        assert store.version_updates == []

    @pytest.mark.asyncio
    async def test_older_version_skips_apply(self) -> None:
        private_key, pubkey_hex = _generate_keypair()
        bundle = _sign_bundle(private_key, _minimal_payload(), version=3, tenant_id="tenant-1")
        store = FakeStore(last_applied_version=10, pubkey_hex=pubkey_hex)

        from hermes.config_sync.applier import PolicyApplier
        apply_called = False
        orig = PolicyApplier.apply

        async def track(self, p, **k):  # noqa: ANN001
            nonlocal apply_called
            apply_called = True
            return ApplyResult()

        PolicyApplier.apply = track
        try:
            with patch("hermes.config_sync.__main__._fetch_bundle", return_value=bundle):
                await _sync_once(store=store, proxy=FakeProxy())
        finally:
            PolicyApplier.apply = orig

        assert not apply_called


# ---------------------------------------------------------------------------
# P0-1 + P0-2: Signature gate — verify FIRST
# ---------------------------------------------------------------------------


class TestSignatureGate:
    @pytest.mark.asyncio
    async def test_invalid_signature_does_not_apply(self) -> None:
        _, pubkey_hex = _generate_keypair()
        wrong_key = Ed25519PrivateKey.generate()
        bundle = _sign_bundle(wrong_key, _minimal_payload(), version=1, tenant_id="tenant-1")
        store = FakeStore(last_applied_version=0, pubkey_hex=pubkey_hex)

        from hermes.config_sync.applier import PolicyApplier
        apply_called = False
        orig = PolicyApplier.apply

        async def track(self, p, **k):  # noqa: ANN001
            nonlocal apply_called
            apply_called = True
            return ApplyResult()

        PolicyApplier.apply = track
        try:
            with patch("hermes.config_sync.__main__._fetch_bundle", return_value=bundle):
                await _sync_once(store=store, proxy=FakeProxy())
        finally:
            PolicyApplier.apply = orig

        assert not apply_called
        assert store.version_updates == []

    @pytest.mark.asyncio
    async def test_bad_sig_logs_signature_rejected_not_tenant_mismatch(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """P0-2: A bad signature on a high-version bundle must produce
        'signature_rejected' log — NOT 'tenant_mismatch'.  The tenant_id
        must only be inspected AFTER the signature is valid."""
        import logging

        _, correct_pubkey = _generate_keypair()
        wrong_key = Ed25519PrivateKey.generate()

        # Sign with wrong key but valid tenant_id so the only failure is the signature.
        bundle = _sign_bundle(
            wrong_key, _minimal_payload(), version=999, tenant_id="tenant-1"
        )
        store = FakeStore(last_applied_version=0, pubkey_hex=correct_pubkey)

        with caplog.at_level(logging.WARNING, logger="hermes.config_sync"):
            with patch("hermes.config_sync.__main__._fetch_bundle", return_value=bundle):
                await _sync_once(store=store, proxy=FakeProxy())

        log_messages = " ".join(caplog.messages)
        assert "signature_rejected" in log_messages
        assert "tenant_mismatch" not in log_messages


# ---------------------------------------------------------------------------
# Successful sync
# ---------------------------------------------------------------------------


class TestSuccessfulSync:
    @pytest.mark.asyncio
    async def test_valid_bundle_advances_version(self) -> None:
        private_key, pubkey_hex = _generate_keypair()
        bundle = _sign_bundle(
            private_key, _minimal_payload(), version=7, tenant_id="tenant-1"
        )
        store = FakeStore(last_applied_version=0, pubkey_hex=pubkey_hex)

        from hermes.config_sync.applier import PolicyApplier
        orig = PolicyApplier.apply

        async def ok_apply(self, p, **k):  # noqa: ANN001
            return ApplyResult(applied=3, failed=[])

        PolicyApplier.apply = ok_apply
        try:
            with patch("hermes.config_sync.__main__._fetch_bundle", return_value=bundle):
                await _sync_once(store=store, proxy=FakeProxy())
        finally:
            PolicyApplier.apply = orig

        assert store.version_updates == [7]
        assert len(store.license_updates) == 1

    @pytest.mark.asyncio
    async def test_partial_failure_does_not_advance_version(self) -> None:
        private_key, pubkey_hex = _generate_keypair()
        bundle = _sign_bundle(
            private_key, _minimal_payload(), version=7, tenant_id="tenant-1"
        )
        store = FakeStore(last_applied_version=0, pubkey_hex=pubkey_hex)

        from hermes.config_sync.applier import PolicyApplier
        orig = PolicyApplier.apply

        async def fail_apply(self, p, **k):  # noqa: ANN001
            return ApplyResult(applied=1, failed=["provider:openai"])

        PolicyApplier.apply = fail_apply
        try:
            with patch("hermes.config_sync.__main__._fetch_bundle", return_value=bundle):
                await _sync_once(store=store, proxy=FakeProxy())
        finally:
            PolicyApplier.apply = orig

        assert store.version_updates == []

    @pytest.mark.asyncio
    async def test_none_bundle_does_nothing(self) -> None:
        store = FakeStore()

        with patch("hermes.config_sync.__main__._fetch_bundle", return_value=None):
            await _sync_once(store=store, proxy=FakeProxy())

        assert store.version_updates == []


# ---------------------------------------------------------------------------
# P1-1: Freshness gate
# ---------------------------------------------------------------------------


class TestFreshnessGate:
    @pytest.mark.asyncio
    async def test_bundle_far_in_future_rejected(self) -> None:
        """issued_at more than 5 min in the future must be rejected."""
        private_key, pubkey_hex = _generate_keypair()
        future_at = _utc_iso(timedelta(minutes=10))  # 10 min future
        bundle = _sign_bundle(
            private_key, _minimal_payload(),
            version=1, tenant_id="tenant-1", issued_at=future_at,
        )
        store = FakeStore(last_applied_version=0, pubkey_hex=pubkey_hex)

        from hermes.config_sync.applier import PolicyApplier
        apply_called = False
        orig = PolicyApplier.apply

        async def track(self, p, **k):  # noqa: ANN001
            nonlocal apply_called
            apply_called = True
            return ApplyResult()

        PolicyApplier.apply = track
        try:
            with patch("hermes.config_sync.__main__._fetch_bundle", return_value=bundle):
                await _sync_once(store=store, proxy=FakeProxy())
        finally:
            PolicyApplier.apply = orig

        assert not apply_called
        assert store.version_updates == []

    @pytest.mark.asyncio
    async def test_bundle_31_days_old_rejected(self) -> None:
        """issued_at more than 30 days in the past must be rejected."""
        private_key, pubkey_hex = _generate_keypair()
        stale_at = _utc_iso(timedelta(days=-31))
        bundle = _sign_bundle(
            private_key, _minimal_payload(),
            version=1, tenant_id="tenant-1", issued_at=stale_at,
        )
        store = FakeStore(last_applied_version=0, pubkey_hex=pubkey_hex)

        from hermes.config_sync.applier import PolicyApplier
        apply_called = False
        orig = PolicyApplier.apply

        async def track(self, p, **k):  # noqa: ANN001
            nonlocal apply_called
            apply_called = True
            return ApplyResult()

        PolicyApplier.apply = track
        try:
            with patch("hermes.config_sync.__main__._fetch_bundle", return_value=bundle):
                await _sync_once(store=store, proxy=FakeProxy())
        finally:
            PolicyApplier.apply = orig

        assert not apply_called
        assert store.version_updates == []

    @pytest.mark.asyncio
    async def test_bundle_within_window_accepted(self) -> None:
        """issued_at within the acceptable window is not rejected by freshness."""
        private_key, pubkey_hex = _generate_keypair()
        recent_at = _utc_iso(timedelta(minutes=-5))  # 5 min ago — within -30d window
        bundle = _sign_bundle(
            private_key, _minimal_payload(),
            version=1, tenant_id="tenant-1", issued_at=recent_at,
        )
        store = FakeStore(last_applied_version=0, pubkey_hex=pubkey_hex)

        from hermes.config_sync.applier import PolicyApplier
        apply_called = False
        orig = PolicyApplier.apply

        async def track(self, p, **k):  # noqa: ANN001
            nonlocal apply_called
            apply_called = True
            return ApplyResult(applied=0)

        PolicyApplier.apply = track
        try:
            with patch("hermes.config_sync.__main__._fetch_bundle", return_value=bundle):
                await _sync_once(store=store, proxy=FakeProxy())
        finally:
            PolicyApplier.apply = orig

        assert apply_called


# ---------------------------------------------------------------------------
# P2: Pubkey validation
# ---------------------------------------------------------------------------


class TestPubkeyValidation:
    @pytest.mark.asyncio
    async def test_empty_pubkey_aborts_before_fetch(self) -> None:
        """P2: empty pubkey_hex must abort before making any network call."""
        store = FakeStore(pubkey_hex="")

        with patch("hermes.config_sync.__main__._fetch_bundle") as mock_fetch:
            await _sync_once(store=store, proxy=FakeProxy())

        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_pubkey_aborts_before_fetch(self) -> None:
        """P2: pubkey_hex shorter than 64 chars is invalid and must abort."""
        store = FakeStore(pubkey_hex="deadbeef")  # only 8 chars

        with patch("hermes.config_sync.__main__._fetch_bundle") as mock_fetch:
            await _sync_once(store=store, proxy=FakeProxy())

        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_hex_pubkey_aborts_before_fetch(self) -> None:
        """P2: pubkey_hex that is not valid hex must abort before fetch."""
        store = FakeStore(pubkey_hex="g" * 64)  # 64 chars but not valid hex

        with patch("hermes.config_sync.__main__._fetch_bundle") as mock_fetch:
            await _sync_once(store=store, proxy=FakeProxy())

        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_64_hex_char_pubkey_proceeds_to_fetch(self) -> None:
        """P2: a syntactically valid pubkey_hex (64 hex chars) passes the gate."""
        # We need a real keypair here to survive verify_bundle.
        private_key, pubkey_hex = _generate_keypair()
        bundle = _sign_bundle(private_key, _minimal_payload(), version=1, tenant_id="tenant-1")
        store = FakeStore(pubkey_hex=pubkey_hex)

        with patch("hermes.config_sync.__main__._fetch_bundle", return_value=bundle) as mock_fetch:
            from hermes.config_sync.applier import PolicyApplier
            orig = PolicyApplier.apply

            async def ok_apply(self, p, **k):  # noqa: ANN001
                return ApplyResult()

            PolicyApplier.apply = ok_apply
            try:
                await _sync_once(store=store, proxy=FakeProxy())
            finally:
                PolicyApplier.apply = orig

        mock_fetch.assert_called_once()
