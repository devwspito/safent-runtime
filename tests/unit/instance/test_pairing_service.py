"""PairingService — unit tests with a FakeControlPlaneClient.

Tests verify:
  - pair() happy path: handshake completes, association persisted,
    NodeEnrollmentService and TenantBindingService both used.
  - pair() second call raises AlreadyAssociatedError.
  - P0: shared_secret is derived from code locally (never from a network response).
    A fake cloud that signs the challenge with KDF(wrong_code) fails at receive_challenge.
  - P1: pubkey binding: absent / bad binding → PubkeyBindingError.
  - P1: pubkey too short / malformed hex → PubkeyBindingError.
  - P2: cloud response missing required fields → PairingError (generic message).
  - P2: response with license > 64 KB → PairingError.
  - P2: nonce not hex → PairingError.
  - pair() with a CodeInvalidError from the client propagates it.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac_mod
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from hermes.agents_os.application.node_enrollment import (
    EnrollmentChallengeMismatch,
    NodeEnrollmentService,
    build_challenge,
)
from hermes.agents_os.application.tenant_binding_service import TenantBindingService
from hermes.instance.association_store import SQLiteAssociationStore
from hermes.instance.pairing_service import (
    AlreadyAssociatedError,
    ChallengeFailedError,
    CodeInvalidError,
    PairingError,
    PairingService,
    PubkeyBindingError,
    _derive_shared_secret,
)
from hermes.shell_server.security import secrets as secrets_mod
from hermes.shell_server.security.secrets import SecretsVault

pytestmark = pytest.mark.unit

_TENANT_ID = UUID("00000000-0000-0000-0000-aabbccddeeff")
_INSTANCE_ID = "aaaabbbb-0000-5000-8000-000000000001"
_INSTANCE_SECRET = "instance-secret-enterprise-xyz-valid"
# A real 32-byte Ed25519 pubkey as hex (64 chars)
_PUBKEY_HEX = "a" * 64

# The canonical pairing code for most tests.  The fake cloud derives
# the shared_secret with the SAME code via _derive_shared_secret().
_PAIR_CODE = "PAIR-CODE-001"


def _make_shared_secret(code: str, tenant_id: UUID = _TENANT_ID) -> bytes:
    """Derive shared_secret the same way PairingService does (P0)."""
    return _derive_shared_secret(code, tenant_id)


def _pubkey_binding(
    shared_secret: bytes, instance_id: str, pubkey_hex: str
) -> str:
    return _hmac_mod.new(
        shared_secret,
        (instance_id + pubkey_hex).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_master_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = tmp_path / "master.key"
    key_file.write_bytes(b"\xee" * 32)
    monkeypatch.setattr(secrets_mod, "_MASTER_KEY_PATH", key_file)


@pytest.fixture
def vault() -> SecretsVault:
    return SecretsVault(master_key=b"\xee" * 32)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "shell-state.db"


@pytest.fixture
def store(db_path: Path, vault: SecretsVault) -> SQLiteAssociationStore:
    return SQLiteAssociationStore(db_path=db_path, vault=vault)


# ------------------------------------------------------------------
# FakeControlPlaneClient
#
# Signs challenges using _derive_shared_secret(code, tenant_id) so the
# PairingService (which derives the secret the same way) can verify.
# ------------------------------------------------------------------


class FakeControlPlaneClient:
    """Implements the ControlPlaneClient Protocol with KDF-derived secrets.

    By default:
      - begin_associate signs the challenge using _derive_shared_secret(code, tenant_id).
      - submit_proof returns a valid pubkey binding.

    Overrides:
      code_override_for_signing: sign the challenge with a different code
        (simulates a MITM that signs with a different key → challenge fails).
      pubkey_binding_override: inject an arbitrary pubkey_binding_hex.
      pubkey_hex_override: inject a custom signing_pubkey_hex.
      raise_on_begin / raise_on_proof: inject exceptions.
    """

    def __init__(
        self,
        *,
        code_override_for_signing: str | None = None,
        pubkey_binding_override: str | None = None,
        pubkey_hex_override: str | None = None,
        raise_on_begin: Exception | None = None,
        raise_on_proof: Exception | None = None,
        license_override: dict | None = None,
        omit_fields: set[str] | None = None,
    ) -> None:
        self._code_override = code_override_for_signing
        self._pubkey_binding_override = pubkey_binding_override
        self._pubkey_hex_override = pubkey_hex_override
        self._raise_on_begin = raise_on_begin
        self._raise_on_proof = raise_on_proof
        self._license_override = license_override
        self._omit_fields = omit_fields or set()
        self.begin_called = False
        self.proof_called = False
        # Capture the code presented by PairingService for assertions.
        self.last_code: str | None = None

    def begin_associate(
        self,
        *,
        code: str,
        instance_id: str,
        hardware_fingerprint: str,
    ) -> dict:
        self.begin_called = True
        self.last_code = code
        if self._raise_on_begin is not None:
            raise self._raise_on_begin
        # Derive the signing key from the code (same KDF as the client).
        signing_code = self._code_override if self._code_override is not None else code
        secret = _make_shared_secret(signing_code)
        ch = build_challenge(
            tenant_id=_TENANT_ID,
            instance_id=UUID(instance_id),
            shared_secret=secret,
            ttl_seconds=60,
        )
        resp: dict = {
            "tenant_id": str(_TENANT_ID),
            "nonce_hex": ch.nonce_hex,
            "challenge_signature_hex": ch.challenge_signature_hex,
            "expires_at_iso": ch.expires_at.isoformat(),
        }
        for field in self._omit_fields:
            resp.pop(field, None)
        return resp

    def submit_proof(
        self,
        *,
        instance_id: str,
        proof_hex: str,
    ) -> dict:
        self.proof_called = True
        if self._raise_on_proof is not None:
            raise self._raise_on_proof
        pubkey = self._pubkey_hex_override if self._pubkey_hex_override is not None else _PUBKEY_HEX
        # Compute correct binding (using the code captured from begin_associate).
        code = self.last_code or _PAIR_CODE
        secret = _make_shared_secret(code)
        binding = _pubkey_binding(secret, instance_id, pubkey)
        resp: dict = {
            "tenant_id": str(_TENANT_ID),
            "instance_secret": _INSTANCE_SECRET,
            "signing_pubkey_hex": pubkey,
            "pubkey_binding_hex": (
                self._pubkey_binding_override
                if self._pubkey_binding_override is not None
                else binding
            ),
            "license": self._license_override if self._license_override is not None else {"plan": "enterprise", "seats": 100},
            "issued_node_cert_hex": "cafebabe",
        }
        for field in self._omit_fields:
            resp.pop(field, None)
        return resp


def _make_service(
    store: SQLiteAssociationStore,
    *,
    client: FakeControlPlaneClient | None = None,
    enrollment: NodeEnrollmentService | None = None,
    binding: TenantBindingService | None = None,
) -> PairingService:
    return PairingService(
        enrollment=enrollment or NodeEnrollmentService(),
        binding=binding or TenantBindingService(),
        store=store,
        client=client or FakeControlPlaneClient(),
    )


# ------------------------------------------------------------------
# Monkeypatch identity — fix return value but accept optional db_path arg.
# ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    import hermes.instance.identity as identity_mod  # noqa: PLC0415

    monkeypatch.setattr(
        identity_mod,
        "resolve_instance_id",
        lambda db_path=None: _INSTANCE_ID,  # noqa: ARG005
    )
    monkeypatch.setattr(
        identity_mod,
        "hardware_fingerprint",
        lambda: "deadbeef" * 8,
    )


# ------------------------------------------------------------------
# Happy path
# ------------------------------------------------------------------


class TestPairHappyPath:
    def test_pair_returns_active_association(self, store: SQLiteAssociationStore) -> None:
        svc = _make_service(store)
        assoc = svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")
        assert assoc.state == "active"
        assert assoc.tenant_id == str(_TENANT_ID)

    def test_pair_creates_enterprise_marker(
        self, store: SQLiteAssociationStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: pairing MUST create the .enterprise marker that gates
        hermes-config-sync.service — otherwise a freshly-paired instance never
        pulls its signed policy and the Enterprise governance loop never starts.
        """
        marker = tmp_path / "instance" / ".enterprise"
        monkeypatch.setenv("HERMES_ENTERPRISE_MARKER", str(marker))
        assert not marker.exists()

        _make_service(store).pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")

        assert marker.exists(), "pairing must create the config-sync gate marker"

    def test_marker_helpers_are_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes.instance.pairing_service import (
            remove_enterprise_marker,
            write_enterprise_marker,
        )

        marker = tmp_path / "instance" / ".enterprise"
        monkeypatch.setenv("HERMES_ENTERPRISE_MARKER", str(marker))
        write_enterprise_marker()
        write_enterprise_marker()  # idempotent — no raise
        assert marker.exists()
        remove_enterprise_marker()
        remove_enterprise_marker()  # idempotent — no raise on missing
        assert not marker.exists()

    def test_pair_persists_association(self, store: SQLiteAssociationStore) -> None:
        svc = _make_service(store)
        svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")
        stored = store.get()
        assert stored is not None
        assert stored.tenant_id == str(_TENANT_ID)

    def test_pair_stores_instance_secret_encrypted(
        self, store: SQLiteAssociationStore, db_path: Path
    ) -> None:
        svc = _make_service(store)
        svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")
        raw = db_path.read_bytes()
        assert _INSTANCE_SECRET.encode() not in raw

    def test_pair_reveals_correct_secret(self, store: SQLiteAssociationStore) -> None:
        svc = _make_service(store)
        svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")
        assert store.reveal_instance_secret() == _INSTANCE_SECRET

    def test_pair_uses_node_enrollment_service(self, store: SQLiteAssociationStore) -> None:
        enrollment = NodeEnrollmentService()
        svc = _make_service(store, enrollment=enrollment)
        svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")
        from hermes.agents_os.application.node_enrollment import EnrollmentState  # noqa: PLC0415
        enrolled = [
            s for s in enrollment._sessions.values()
            if s.state == EnrollmentState.ENROLLED
        ]
        assert len(enrolled) == 1

    def test_pair_uses_tenant_binding_service(self, store: SQLiteAssociationStore) -> None:
        binding = TenantBindingService()
        svc = _make_service(store, binding=binding)
        svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")
        assert binding.has_active_binding(
            node_installation_id=UUID(_INSTANCE_ID)
        )

    def test_pair_calls_both_client_methods(self, store: SQLiteAssociationStore) -> None:
        client = FakeControlPlaneClient()
        svc = _make_service(store, client=client)
        svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")
        assert client.begin_called
        assert client.proof_called

    def test_pair_persists_pubkey(self, store: SQLiteAssociationStore) -> None:
        svc = _make_service(store)
        assoc = svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")
        assert assoc.signing_pubkey_hex == _PUBKEY_HEX


# ------------------------------------------------------------------
# P0 — shared_secret derived from code, never received over the wire
# ------------------------------------------------------------------


class TestSharedSecretDerivation:
    def test_wrong_code_fails_challenge_verification(
        self, store: SQLiteAssociationStore
    ) -> None:
        """The fake cloud signs with KDF(wrong_code); the client derives
        KDF(correct_code) → mismatch → EnrollmentChallengeMismatch."""
        client = FakeControlPlaneClient(code_override_for_signing="WRONG-CODE")
        svc = _make_service(store, client=client)
        with pytest.raises(EnrollmentChallengeMismatch):
            svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")

    def test_correct_code_passes(self, store: SQLiteAssociationStore) -> None:
        """Same code on both sides → handshake succeeds."""
        svc = _make_service(store)
        assoc = svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")
        assert assoc.state == "active"

    def test_derive_shared_secret_is_deterministic(self) -> None:
        secret1 = _derive_shared_secret(_PAIR_CODE, _TENANT_ID)
        secret2 = _derive_shared_secret(_PAIR_CODE, _TENANT_ID)
        assert secret1 == secret2

    def test_derive_shared_secret_changes_with_code(self) -> None:
        s1 = _derive_shared_secret("CODE-A", _TENANT_ID)
        s2 = _derive_shared_secret("CODE-B", _TENANT_ID)
        assert s1 != s2

    def test_derive_shared_secret_changes_with_tenant_id(self) -> None:
        t1 = UUID("00000000-0000-0000-0000-000000000001")
        t2 = UUID("00000000-0000-0000-0000-000000000002")
        s1 = _derive_shared_secret(_PAIR_CODE, t1)
        s2 = _derive_shared_secret(_PAIR_CODE, t2)
        assert s1 != s2

    def test_derive_shared_secret_returns_32_bytes(self) -> None:
        secret = _derive_shared_secret(_PAIR_CODE, _TENANT_ID)
        assert len(secret) == 32


# ------------------------------------------------------------------
# P1 — pubkey binding
# ------------------------------------------------------------------


class TestPubkeyBinding:
    def test_missing_pubkey_binding_raises(self, store: SQLiteAssociationStore) -> None:
        client = FakeControlPlaneClient(omit_fields={"pubkey_binding_hex"})
        svc = _make_service(store, client=client)
        with pytest.raises(PairingError):
            svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")

    def test_wrong_pubkey_binding_raises(self, store: SQLiteAssociationStore) -> None:
        client = FakeControlPlaneClient(pubkey_binding_override="deadbeef" * 8)
        svc = _make_service(store, client=client)
        with pytest.raises(PubkeyBindingError):
            svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")

    def test_pubkey_too_short_raises(self, store: SQLiteAssociationStore) -> None:
        client = FakeControlPlaneClient(pubkey_hex_override="aabb")
        svc = _make_service(store, client=client)
        with pytest.raises(PubkeyBindingError):
            svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")

    def test_pubkey_not_hex_raises(self, store: SQLiteAssociationStore) -> None:
        client = FakeControlPlaneClient(pubkey_hex_override="z" * 64)
        svc = _make_service(store, client=client)
        with pytest.raises(PubkeyBindingError):
            svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")

    def test_empty_pubkey_raises(self, store: SQLiteAssociationStore) -> None:
        client = FakeControlPlaneClient(pubkey_hex_override="")
        svc = _make_service(store, client=client)
        with pytest.raises(PubkeyBindingError):
            svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")


# ------------------------------------------------------------------
# P2 — cloud response validation
# ------------------------------------------------------------------


class TestCloudResponseValidation:
    def test_begin_response_missing_tenant_id_raises(
        self, store: SQLiteAssociationStore
    ) -> None:
        client = FakeControlPlaneClient(omit_fields={"tenant_id"})
        svc = _make_service(store, client=client)
        with pytest.raises(PairingError):
            svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")

    def test_begin_response_missing_nonce_raises(
        self, store: SQLiteAssociationStore
    ) -> None:
        client = FakeControlPlaneClient(omit_fields={"nonce_hex"})
        svc = _make_service(store, client=client)
        with pytest.raises(PairingError):
            svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")

    def test_proof_response_missing_instance_secret_raises(
        self, store: SQLiteAssociationStore
    ) -> None:
        client = FakeControlPlaneClient(omit_fields={"instance_secret"})
        svc = _make_service(store, client=client)
        with pytest.raises(PairingError):
            svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")

    def test_proof_response_giant_license_raises(
        self, store: SQLiteAssociationStore
    ) -> None:
        giant_license = {"data": "x" * 70_000}
        client = FakeControlPlaneClient(license_override=giant_license)
        svc = _make_service(store, client=client)
        with pytest.raises(PairingError):
            svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")


# ------------------------------------------------------------------
# Idempotency: second call raises AlreadyAssociatedError
# ------------------------------------------------------------------


class TestAlreadyAssociated:
    def test_second_pair_raises(self, store: SQLiteAssociationStore) -> None:
        svc = _make_service(store)
        svc.pair(code=_PAIR_CODE, cloud_endpoint="https://fake.cloud")
        with pytest.raises(AlreadyAssociatedError):
            svc.pair(code="PAIR-CODE-002", cloud_endpoint="https://fake.cloud")


# ------------------------------------------------------------------
# Code rejected by control plane
# ------------------------------------------------------------------


class TestCodeInvalid:
    def test_code_invalid_error_propagates(self, store: SQLiteAssociationStore) -> None:
        client = FakeControlPlaneClient(
            raise_on_begin=CodeInvalidError("código expirado")
        )
        svc = _make_service(store, client=client)
        with pytest.raises(CodeInvalidError):
            svc.pair(code="INVALID-CODE", cloud_endpoint="https://fake.cloud")

    def test_no_association_persisted_on_begin_failure(
        self, store: SQLiteAssociationStore
    ) -> None:
        client = FakeControlPlaneClient(
            raise_on_begin=CodeInvalidError("code not found")
        )
        svc = _make_service(store, client=client)
        with pytest.raises(CodeInvalidError):
            svc.pair(code="INVALID-CODE", cloud_endpoint="https://fake.cloud")
        assert store.is_associated() is False
