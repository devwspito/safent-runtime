"""NodeEnrollment — bootstrap del nodo contra el control plane (FR-007).

Tras finalize() del wizard, el nodo debe registrarse con el control
plane del operador (Cloud SaaS) o con su propio control plane self-hosted.

Cloud SaaS: control plane = endpoint Hermes-operated; el nodo recibe
keys de enrollment temporales firmadas por la CA del tenant.

Self-hosted: control plane = el propio server local; enrollment es
trivial (localhost + key file).

Estados:
  NOT_ENROLLED → REQUESTED → CHALLENGE_RECEIVED → CHALLENGE_SOLVED
  → ENROLLED → REVOKED

Challenge-response: el control plane envía un nonce firmado HMAC con
shared_secret + tenant_id; el nodo responde con la prueba de posesión
del hardware fingerprint. Esto evita replay con un enrollment token
robado.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4


class NodeEnrollmentError(RuntimeError):
    pass


class EnrollmentChallengeMismatch(NodeEnrollmentError):
    """HMAC inválido — challenge no verifica con el shared_secret."""


class EnrollmentStateInvalid(NodeEnrollmentError):
    pass


class EnrollmentExpired(NodeEnrollmentError):
    pass


class OperationalModel(StrEnum):
    CLOUD_SAAS_MANAGED = "cloud_saas_managed"
    SELF_HOSTED = "self_hosted"


class EnrollmentState(StrEnum):
    NOT_ENROLLED = "not_enrolled"
    REQUESTED = "requested"
    CHALLENGE_RECEIVED = "challenge_received"
    CHALLENGE_SOLVED = "challenge_solved"
    ENROLLED = "enrolled"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class EnrollmentChallenge:
    """Reto del control plane (firmado HMAC sobre nonce+tenant_id)."""

    nonce_hex: str
    tenant_id: UUID
    challenge_signature_hex: str  # HMAC del CP sobre nonce+tenant_id
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class EnrollmentSession:
    enrollment_id: UUID
    node_installation_id: UUID
    operational_model: OperationalModel
    control_plane_endpoint: str
    state: EnrollmentState
    requested_at: datetime
    enrolled_at: datetime | None
    revoked_at: datetime | None
    issued_node_cert_hex: str | None
    hardware_fingerprint: str
    last_challenge: EnrollmentChallenge | None
    tenant_id: UUID | None


class NodeEnrollmentService:
    """Aplica el handshake de enrollment al control plane.

    El cifrado/transporte real (mTLS) está en infrastructure; aquí solo
    el state machine + verificación HMAC.
    """

    def __init__(self) -> None:
        self._sessions: dict[UUID, EnrollmentSession] = {}

    def request_enrollment(
        self,
        *,
        node_installation_id: UUID,
        operational_model: OperationalModel,
        control_plane_endpoint: str,
        hardware_fingerprint: str,
        tenant_id: UUID | None = None,
    ) -> EnrollmentSession:
        eid = uuid4()
        snap = EnrollmentSession(
            enrollment_id=eid,
            node_installation_id=node_installation_id,
            operational_model=operational_model,
            control_plane_endpoint=control_plane_endpoint,
            state=EnrollmentState.REQUESTED,
            requested_at=datetime.now(tz=UTC),
            enrolled_at=None,
            revoked_at=None,
            issued_node_cert_hex=None,
            hardware_fingerprint=hardware_fingerprint,
            last_challenge=None,
            tenant_id=tenant_id,
        )
        self._sessions[eid] = snap
        return snap

    def receive_challenge(
        self,
        *,
        enrollment_id: UUID,
        challenge: EnrollmentChallenge,
        shared_secret: bytes,
    ) -> EnrollmentSession:
        current = self._fetch(enrollment_id)
        if current.state != EnrollmentState.REQUESTED:
            raise EnrollmentStateInvalid(
                f"receive_challenge requiere REQUESTED, está {current.state}"
            )
        # Verificar firma del CP.
        expected = hmac.new(
            shared_secret,
            bytes.fromhex(challenge.nonce_hex)
            + challenge.tenant_id.bytes,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(
            expected, challenge.challenge_signature_hex
        ):
            raise EnrollmentChallengeMismatch(
                "challenge_signature inválida — posible MITM o tenant_id mismatch"
            )
        snap = replace(
            current,
            state=EnrollmentState.CHALLENGE_RECEIVED,
            last_challenge=challenge,
            tenant_id=challenge.tenant_id,
        )
        self._sessions[enrollment_id] = snap
        return snap

    def solve_challenge(
        self,
        *,
        enrollment_id: UUID,
        shared_secret: bytes,
    ) -> tuple[EnrollmentSession, str]:
        """Devuelve la prueba HMAC = HMAC(secret, nonce || hardware_fp)."""
        current = self._fetch(enrollment_id)
        if current.state != EnrollmentState.CHALLENGE_RECEIVED:
            raise EnrollmentStateInvalid(
                f"solve_challenge requiere CHALLENGE_RECEIVED, está {current.state}"
            )
        ch = current.last_challenge
        assert ch is not None
        now = datetime.now(tz=UTC)
        if now > ch.expires_at:
            raise EnrollmentExpired("challenge expirado")
        proof = hmac.new(
            shared_secret,
            bytes.fromhex(ch.nonce_hex)
            + current.hardware_fingerprint.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        snap = replace(current, state=EnrollmentState.CHALLENGE_SOLVED)
        self._sessions[enrollment_id] = snap
        return snap, proof

    def complete_enrollment(
        self,
        *,
        enrollment_id: UUID,
        issued_node_cert_hex: str,
    ) -> EnrollmentSession:
        current = self._fetch(enrollment_id)
        if current.state != EnrollmentState.CHALLENGE_SOLVED:
            raise EnrollmentStateInvalid(
                f"complete requiere CHALLENGE_SOLVED, está {current.state}"
            )
        snap = replace(
            current,
            state=EnrollmentState.ENROLLED,
            enrolled_at=datetime.now(tz=UTC),
            issued_node_cert_hex=issued_node_cert_hex,
        )
        self._sessions[enrollment_id] = snap
        return snap

    def revoke(self, *, enrollment_id: UUID, reason: str) -> EnrollmentSession:
        current = self._fetch(enrollment_id)
        snap = replace(
            current,
            state=EnrollmentState.REVOKED,
            revoked_at=datetime.now(tz=UTC),
        )
        self._sessions[enrollment_id] = snap
        return snap

    def _fetch(self, eid: UUID) -> EnrollmentSession:
        if eid not in self._sessions:
            raise EnrollmentStateInvalid(f"unknown enrollment {eid}")
        return self._sessions[eid]


def build_challenge(
    *,
    tenant_id: UUID,
    shared_secret: bytes,
    ttl_seconds: int = 60,
) -> EnrollmentChallenge:
    """Helper de testing: produce un challenge firmado."""
    nonce = secrets.token_bytes(32)
    sig = hmac.new(
        shared_secret, nonce + tenant_id.bytes, hashlib.sha256
    ).hexdigest()
    now = datetime.now(tz=UTC)
    return EnrollmentChallenge(
        nonce_hex=nonce.hex(),
        tenant_id=tenant_id,
        challenge_signature_hex=sig,
        issued_at=now,
        expires_at=now.fromtimestamp(now.timestamp() + ttl_seconds, tz=UTC),
    )
