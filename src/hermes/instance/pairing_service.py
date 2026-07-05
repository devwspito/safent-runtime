"""PairingService — orchestrates the enterprise pairing handshake.

Flow:
  1. request_enrollment (NodeEnrollmentService) — create a local enrollment session.
  2. begin_associate (ControlPlaneClient) — send code + instance_id + fingerprint
     to the control plane; receive the HMAC challenge (nonce + tenant_id + sig).
  3. _derive_shared_secret — derive the HMAC key locally from the code and the
     tenant_id returned by the CP.  The key NEVER travels over the network.
  4. receive_challenge (NodeEnrollmentService) — verify the CP's HMAC signature
     with the locally derived key.
  5. solve_challenge (NodeEnrollmentService) — compute the proof HMAC.
  6. submit_proof (ControlPlaneClient) — send the proof; receive the association
     payload including the pubkey binding.
  7. _verify_pubkey_binding — verify HMAC(shared_secret, instance_id || pubkey_hex)
     before trusting the pubkey.
  8. complete_enrollment (NodeEnrollmentService) — mark ENROLLED.
  9. binding.bind (TenantBindingService) — record the tenant binding in memory.
  10. store.save (SQLiteAssociationStore) — persist the association with the
      encrypted instance_secret.

Security design of the shared_secret:
  The shared_secret is NEVER transmitted.  Both sides derive it independently:
    HKDF-SHA256(ikm=code.encode(), salt=tenant_id.bytes,
                info=b"safent-pairing-shared-secret-v1", length=32)
  The cloud derives it from the code it originally issued; the client derives it
  from the code the operator entered.  A MITM or replay cannot obtain the secret
  without knowing the original code.

Pairing code entropy requirements (cloud responsibility, documented here):
  - Minimum recommended: 12 chars Crockford-base32 ≈ 60 bits of entropy.
  - Single-use: invalidated server-side after first successful pair().
  - TTL ≤ 10 minutes: expired codes must be rejected by the CP.
  - Lockout: CP must enforce per-code brute-force lockout (e.g. 5 attempts).

Error hierarchy:
  PairingError (base)
  ├── AlreadyAssociatedError
  ├── CodeInvalidError
  ├── ChallengeFailedError
  └── PubkeyBindingError
"""

from __future__ import annotations

import binascii
import hashlib
import hmac as _hmac_mod
import logging
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from hermes.agents_os.application.node_enrollment import (
    EnrollmentChallenge,
    NodeEnrollmentService,
    OperationalModel,
)
from hermes.agents_os.application.tenant_binding_service import (
    TenantBindingService,
)
from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore

logger = logging.getLogger("hermes.instance.pairing_service")

# Minimum pubkey length in hex chars (1 byte = 2 hex chars; 32-byte pubkey = 64 chars).
# Set conservatively: a real signing key is at least 32 bytes (Ed25519 = 64 hex chars).
_MIN_PUBKEY_HEX_LEN = 64


# ------------------------------------------------------------------
# Errors
# ------------------------------------------------------------------


class PairingError(RuntimeError):
    """Base class for all pairing errors."""


class AlreadyAssociatedError(PairingError):
    """Raised when pair() is called on an already-associated instance."""


class CodeInvalidError(PairingError):
    """Raised when the control plane rejects the pairing code."""


class ChallengeFailedError(PairingError):
    """Raised when the HMAC challenge verification fails."""


class PubkeyBindingError(PairingError):
    """Raised when the pubkey binding HMAC is absent or invalid."""


# ------------------------------------------------------------------
# Pydantic models for cloud response validation (P2)
# ------------------------------------------------------------------


def _parse_begin_response(raw: dict) -> dict:
    """Validate begin_associate response fields.

    Raises PairingError with a generic message on any validation failure
    (never echoes raw cloud content to the caller).
    """
    required = {"tenant_id", "nonce_hex", "challenge_signature_hex", "expires_at_iso"}
    missing = required - raw.keys()
    if missing:
        logger.warning(
            "hermes.instance.begin_response.invalid",
            extra={"missing": sorted(missing)},
        )
        raise PairingError("El control plane devolvió una respuesta inválida (begin).")

    # Validate nonce_hex is hex
    try:
        bytes.fromhex(raw["nonce_hex"])
    except ValueError:
        logger.warning("hermes.instance.begin_response.nonce_not_hex")
        raise PairingError("El control plane devolvió un nonce malformado.")

    # Validate challenge_signature_hex is hex
    try:
        bytes.fromhex(raw["challenge_signature_hex"])
    except ValueError:
        logger.warning("hermes.instance.begin_response.sig_not_hex")
        raise PairingError("El control plane devolvió una firma malformada.")

    # Validate tenant_id is a UUID
    try:
        UUID(raw["tenant_id"])
    except (ValueError, AttributeError):
        logger.warning("hermes.instance.begin_response.tenant_id_invalid")
        raise PairingError("El control plane devolvió un tenant_id inválido.")

    # Validate expires_at_iso is parseable
    try:
        datetime.fromisoformat(raw["expires_at_iso"])
    except (ValueError, TypeError):
        logger.warning("hermes.instance.begin_response.expires_invalid")
        raise PairingError("El control plane devolvió un expires_at inválido.")

    return raw


def _parse_proof_response(raw: dict) -> dict:
    """Validate submit_proof response fields.

    Raises PairingError with a generic message on any validation failure.
    Never echoes raw cloud content to the caller (P2: no stack traces / raw
    cloud text in client-visible error messages).
    """
    required = {
        "tenant_id", "instance_secret", "signing_pubkey_hex",
        "pubkey_binding_hex", "issued_node_cert_hex",
    }
    missing = required - raw.keys()
    if missing:
        logger.warning(
            "hermes.instance.proof_response.invalid",
            extra={"missing": sorted(missing)},
        )
        raise PairingError("El control plane devolvió una respuesta inválida (proof).")

    # Validate tenant_id
    try:
        UUID(raw["tenant_id"])
    except (ValueError, AttributeError):
        raise PairingError("El control plane devolvió un tenant_id inválido.")

    # Validate instance_secret length (max 512 chars to avoid DoS)
    if not isinstance(raw["instance_secret"], str) or len(raw["instance_secret"]) > 512:
        logger.warning("hermes.instance.proof_response.secret_too_long")
        raise PairingError("El control plane devolvió un instance_secret inválido.")
    if len(raw["instance_secret"]) < 16:
        raise PairingError("El control plane devolvió un instance_secret demasiado corto.")

    # Validate signing_pubkey_hex is valid hex of minimum length
    pubkey_hex = raw["signing_pubkey_hex"]
    if not isinstance(pubkey_hex, str) or len(pubkey_hex) < _MIN_PUBKEY_HEX_LEN:
        logger.warning(
            "hermes.instance.proof_response.pubkey_too_short",
            extra={"len": len(pubkey_hex) if isinstance(pubkey_hex, str) else -1},
        )
        raise PubkeyBindingError("La clave pública de firma es demasiado corta o está ausente.")
    try:
        binascii.unhexlify(pubkey_hex)
    except (ValueError, binascii.Error):
        logger.warning("hermes.instance.proof_response.pubkey_not_hex")
        raise PubkeyBindingError("La clave pública de firma no es hexadecimal válido.")

    # Validate pubkey_binding_hex is hex
    try:
        bytes.fromhex(raw["pubkey_binding_hex"])
    except (ValueError, KeyError):
        logger.warning("hermes.instance.proof_response.binding_not_hex")
        raise PubkeyBindingError("El binding de la clave pública no es válido.")

    # Cap license size to 64 KB (arbitrary JSON; avoid memory bomb)
    import json  # noqa: PLC0415
    license_data = raw.get("license", {})
    if not isinstance(license_data, dict):
        raise PairingError("El campo 'license' debe ser un objeto JSON.")
    license_json = json.dumps(license_data)
    if len(license_json) > 65_536:
        logger.warning("hermes.instance.proof_response.license_too_large")
        raise PairingError("El campo 'license' supera el tamaño máximo permitido.")

    return raw


# ------------------------------------------------------------------
# Key derivation (P0 fix: secret never transmitted)
# ------------------------------------------------------------------


def _derive_shared_secret(code: str, tenant_id: UUID) -> bytes:
    """Derive the HMAC shared_secret locally from the pairing code and tenant_id.

    HKDF-SHA256(ikm=code.encode("utf-8"), salt=tenant_id.bytes,
                info=b"safent-pairing-shared-secret-v1", length=32)

    The cloud derives the SAME key from the code it originally issued.
    The key NEVER travels over the network — only the HMAC outputs do.

    Reuses _hkdf_derive from hermes.shell_server.security.secrets (RFC 5869
    extract + expand, not a bare SHA-256 round) so the KDF is consistent with
    the rest of the codebase.
    """
    from hermes.shell_server.security.secrets import _hkdf_derive  # noqa: PLC0415

    return _hkdf_derive(
        ikm=code.encode("utf-8"),
        info=b"safent-pairing-shared-secret-v1",
        salt=tenant_id.bytes,
        length=32,
    )


def _verify_pubkey_binding(
    *,
    shared_secret: bytes,
    instance_id: str,
    signing_pubkey_hex: str,
    pubkey_binding_hex: str,
) -> None:
    """Verify HMAC(shared_secret, instance_id || signing_pubkey_hex).

    This binds the pubkey to both the pairing session (via shared_secret
    derived from the one-time code) and the specific instance (instance_id),
    preventing a MITM from substituting their own pubkey.

    Raises PubkeyBindingError on failure.
    """
    expected = _hmac_mod.new(
        shared_secret,
        (instance_id + signing_pubkey_hex).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    provided = pubkey_binding_hex
    if not _hmac_mod.compare_digest(expected, provided):
        logger.warning("hermes.instance.pubkey_binding.mismatch")
        raise PubkeyBindingError(
            "El binding de la clave pública de firma no verifica. "
            "Posible MITM o respuesta manipulada."
        )


# ------------------------------------------------------------------
# ControlPlaneClient protocol
# ------------------------------------------------------------------


class ControlPlaneClient(Protocol):
    """Port: cloud control plane transport.

    Concrete implementations live in hermes.instance.infrastructure.
    The fake for tests is in tests/unit/instance/.
    """

    def begin_associate(
        self,
        *,
        code: str,
        instance_id: str,
        hardware_fingerprint: str,
    ) -> dict:
        """Submit the pairing code and instance identity.

        Returns a dict with keys:
          tenant_id (str UUID)
          nonce_hex (str hex)
          challenge_signature_hex (str hex)
          expires_at_iso (str ISO-8601)

        NOTE: shared_secret is NOT part of this response.  Both sides derive
        it independently from the code using HKDF-SHA256.

        Raises CodeInvalidError when the code is invalid or expired.
        """
        ...

    def submit_proof(
        self,
        *,
        instance_id: str,
        proof_hex: str,
    ) -> dict:
        """Submit the HMAC proof and receive the association payload.

        Returns a dict with keys:
          tenant_id (str UUID)
          instance_secret (str, 16–512 chars)
          signing_pubkey_hex (str hex, min 64 chars / 32 bytes)
          pubkey_binding_hex (str hex) — HMAC(shared_secret, instance_id || pubkey_hex)
          license (dict, max 64 KB JSON)
          issued_node_cert_hex (str hex)

        Raises ChallengeFailedError when the proof is rejected.
        """
        ...


# ------------------------------------------------------------------
# PairingService
# ------------------------------------------------------------------


class PairingService:
    """Orchestrates the full pairing handshake.

    All dependencies are injected; this class has no I/O of its own
    (I/O is in the ControlPlaneClient and the store).
    """

    def __init__(
        self,
        *,
        enrollment: NodeEnrollmentService,
        binding: TenantBindingService,
        store: SQLiteAssociationStore,
        client: ControlPlaneClient,
    ) -> None:
        self._enrollment = enrollment
        self._binding = binding
        self._store = store
        self._client = client

    def pair(self, *, code: str, cloud_endpoint: str) -> InstanceAssociation:
        """Execute the full pairing handshake and persist the result.

        Idempotent at the guard level: raises AlreadyAssociatedError if the
        instance is already active.  The caller is responsible for displaying
        the error to the operator.
        """
        if self._store.is_associated():
            raise AlreadyAssociatedError(
                "Esta instancia ya está asociada a un tenant. "
                "Ejecuta 'safent unpair' antes de volver a vincular."
            )

        from hermes.instance.identity import hardware_fingerprint, resolve_instance_id  # noqa: PLC0415

        # Pass the store's DB path so the random instance_id is persisted once
        # on first pair and reused on subsequent calls (P3).
        instance_id = resolve_instance_id(self._store.db_path)
        fp = hardware_fingerprint()

        logger.info(
            "hermes.instance.pairing.start",
            extra={"instance_id": instance_id, "cloud_endpoint": cloud_endpoint},
        )

        session = self._enrollment.request_enrollment(
            node_installation_id=UUID(instance_id),
            operational_model=OperationalModel.CLOUD_SAAS_MANAGED,
            control_plane_endpoint=cloud_endpoint,
            hardware_fingerprint=fp,
        )

        raw_begin = self._client.begin_associate(
            code=code,
            instance_id=instance_id,
            hardware_fingerprint=fp,
        )
        cp_response = _parse_begin_response(raw_begin)

        tenant_id = UUID(cp_response["tenant_id"])
        # P0: derive shared_secret locally — never transmitted over the wire.
        shared_secret = _derive_shared_secret(code, tenant_id)

        challenge = _build_challenge(cp_response)

        session = self._enrollment.receive_challenge(
            enrollment_id=session.enrollment_id,
            challenge=challenge,
            shared_secret=shared_secret,
        )

        session, proof_hex = self._enrollment.solve_challenge(
            enrollment_id=session.enrollment_id,
            shared_secret=shared_secret,
        )

        raw_proof = self._client.submit_proof(
            instance_id=instance_id,
            proof_hex=proof_hex,
        )
        proof_response = _parse_proof_response(raw_proof)

        # P1: verify pubkey binding before trusting the pubkey.
        _verify_pubkey_binding(
            shared_secret=shared_secret,
            instance_id=instance_id,
            signing_pubkey_hex=proof_response["signing_pubkey_hex"],
            pubkey_binding_hex=proof_response["pubkey_binding_hex"],
        )

        session = self._enrollment.complete_enrollment(
            enrollment_id=session.enrollment_id,
            issued_node_cert_hex=proof_response.get("issued_node_cert_hex", ""),
        )

        self._binding.bind(
            node_installation_id=UUID(instance_id),
            tenant_id=tenant_id,
            tenant_provided_endpoint=cloud_endpoint,
        )

        association = InstanceAssociation(
            instance_id=instance_id,
            tenant_id=str(tenant_id),
            paired_at=datetime.now(tz=UTC).isoformat(),
            cloud_endpoint=cloud_endpoint,
            signing_pubkey_hex=proof_response["signing_pubkey_hex"],
            license=proof_response.get("license", {}),
            last_applied_version=0,
            state="active",
        )
        self._store.save(
            association=association,
            instance_secret=proof_response["instance_secret"],
        )

        logger.info(
            "hermes.instance.pairing.complete",
            extra={"tenant_id": str(tenant_id), "instance_id": instance_id},
        )
        return association


# ------------------------------------------------------------------
# Module-level helpers (also used by tests via import)
# ------------------------------------------------------------------


def _build_challenge(cp_response: dict) -> EnrollmentChallenge:
    """Map the control-plane response dict to an EnrollmentChallenge."""
    return EnrollmentChallenge(
        nonce_hex=cp_response["nonce_hex"],
        tenant_id=UUID(cp_response["tenant_id"]),
        challenge_signature_hex=cp_response["challenge_signature_hex"],
        issued_at=datetime.now(tz=UTC),
        expires_at=datetime.fromisoformat(cp_response["expires_at_iso"]),
    )
