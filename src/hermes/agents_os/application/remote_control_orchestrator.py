"""RemoteControlOrchestrator — sesiones SO-level (FR-053..FR-056 BLOQUEANTES).

El control remoto vive a nivel SO (research §7) — sobrevive a un crash
de Chromium. Capacidad concedida por capability `remote_control_subscription`
+ aprobación HITL local (FR-053).

Estados:
  ISSUED → ACCEPTED → ACTIVE → ENDED (Operador / Operador local /
                                       timeout / tenant_revoked)

Token de sesión:
  - AES-GCM-256 encrypted at-rest (FR-055).
  - AAD = node_installation_id || tenant_id || operator_id || scope.
  - TTL <= 60 min (FR-054).

DTLS fingerprint binding (FR-056): cualquier reconexión con un
fingerprint distinto al inicial mata la sesión.
"""

from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4


class RemoteControlScope(StrEnum):
    OS_FULL_DESKTOP = "os_full_desktop"
    WORKSPACE_BROWSER_ONLY = "workspace_browser_only"


class RemoteControlState(StrEnum):
    ISSUED = "issued"
    ACCEPTED = "accepted"
    ACTIVE = "active"
    ENDED = "ended"


class RemoteControlEndReason(StrEnum):
    OPERATOR_ENDED = "operator_ended"
    LOCAL_OPERATOR_ENDED = "local_operator_ended"
    TIMEOUT = "timeout"
    TENANT_REVOKED = "tenant_revoked"
    BINDING_VIOLATED = "binding_violated"
    CONSENT_REVOKED = "consent_revoked"


_MAX_TTL = timedelta(minutes=60)


class RemoteControlError(RuntimeError):
    pass


class HumanConsentMissingError(RemoteControlError):
    """FR-053: operador local no aprobó la sesión."""


class TtlTooLongError(RemoteControlError):
    """FR-054: TTL solicitado > 60 min."""


class BindingViolationError(RemoteControlError):
    """FR-056: DTLS fingerprint mismatch."""


@dataclass(frozen=True, slots=True)
class RemoteControlSession:
    session_id: UUID
    node_installation_id: UUID
    tenant_id: UUID
    operator_id: UUID
    scope: RemoteControlScope
    state: RemoteControlState
    issued_at: datetime
    accepted_at: datetime | None
    ended_at: datetime | None
    end_reason: RemoteControlEndReason | None
    token_ciphertext: bytes
    token_kid: str
    token_expires_at: datetime
    dtls_fingerprint: str
    binding_hash_hex: str
    consent_id: UUID
    captured_training_steps_count: int


def _binding_hash(
    *,
    node_installation_id: UUID,
    tenant_id: UUID,
    operator_id: UUID,
    scope: RemoteControlScope,
    dtls_fingerprint: str,
) -> str:
    """SHA-256 sobre el quintuple — pinned al iniciar la sesión."""
    import hashlib

    h = hashlib.sha256()
    h.update(node_installation_id.bytes)
    h.update(tenant_id.bytes)
    h.update(operator_id.bytes)
    h.update(scope.value.encode("ascii"))
    h.update(dtls_fingerprint.encode("ascii"))
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class EncryptedToken:
    """Token cifrado AES-GCM-256 con AAD."""

    ciphertext: bytes
    kid: str


class TokenCipher:
    """Wrap AES-GCM-256 con AAD para tokens at-rest (FR-055).

    Args:
        key: clave AES-GCM-256 (32 bytes).
        kid: identificador de clave para rotación.
    """

    def __init__(self, *, key: bytes, kid: str) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if len(key) != 32:
            raise ValueError("AES-GCM-256 requiere key de 32 bytes")
        self._aead = AESGCM(key)
        self._kid = kid

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> EncryptedToken:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, plaintext, aad)
        return EncryptedToken(ciphertext=nonce + ct, kid=self._kid)

    def decrypt(
        self, ciphertext: bytes, *, aad: bytes, expected_kid: str
    ) -> bytes:
        if expected_kid != self._kid:
            raise RemoteControlError(
                f"kid mismatch: expected={self._kid} got={expected_kid}"
            )
        if len(ciphertext) < 13:
            raise RemoteControlError("ciphertext truncado")
        nonce, payload = ciphertext[:12], ciphertext[12:]
        return self._aead.decrypt(nonce, payload, aad)


@dataclass(slots=True)
class RemoteControlOrchestrator:
    """Crea sesiones de control remoto SO-level firmadas y cifradas."""

    cipher: TokenCipher
    clock: object = None

    def __post_init__(self) -> None:
        if self.clock is None:
            self.clock = lambda: datetime.now(tz=UTC)

    def issue(
        self,
        *,
        node_installation_id: UUID,
        tenant_id: UUID,
        operator_id: UUID,
        scope: RemoteControlScope,
        dtls_fingerprint: str,
        consent_id: UUID,
        local_operator_approved: bool,
        ttl_seconds: int,
    ) -> RemoteControlSession:
        if not local_operator_approved:
            raise HumanConsentMissingError(
                "FR-053: operador local debe aprobar la sesión"
            )
        if ttl_seconds <= 0 or ttl_seconds > _MAX_TTL.total_seconds():
            raise TtlTooLongError(
                f"FR-054: ttl_seconds debe estar en (0, {int(_MAX_TTL.total_seconds())}]"
            )
        now = self.clock()
        expires_at = now + timedelta(seconds=ttl_seconds)

        binding_hex = _binding_hash(
            node_installation_id=node_installation_id,
            tenant_id=tenant_id,
            operator_id=operator_id,
            scope=scope,
            dtls_fingerprint=dtls_fingerprint,
        )
        aad = binding_hex.encode("ascii")
        plaintext = secrets.token_bytes(48)  # entropy del bearer
        token = self.cipher.encrypt(plaintext, aad=aad)

        return RemoteControlSession(
            session_id=uuid4(),
            node_installation_id=node_installation_id,
            tenant_id=tenant_id,
            operator_id=operator_id,
            scope=scope,
            state=RemoteControlState.ISSUED,
            issued_at=now,
            accepted_at=None,
            ended_at=None,
            end_reason=None,
            token_ciphertext=token.ciphertext,
            token_kid=token.kid,
            token_expires_at=expires_at,
            dtls_fingerprint=dtls_fingerprint,
            binding_hash_hex=binding_hex,
            consent_id=consent_id,
            captured_training_steps_count=0,
        )

    def verify_reconnect_binding(
        self,
        session: RemoteControlSession,
        *,
        observed_dtls_fingerprint: str,
    ) -> None:
        """FR-056: una reconexión con fingerprint distinto = sesión muerta."""
        if not hmac.compare_digest(
            session.dtls_fingerprint, observed_dtls_fingerprint
        ):
            raise BindingViolationError(
                "DTLS fingerprint cambió — sesión revocada"
            )

    def accept(
        self, session: RemoteControlSession
    ) -> RemoteControlSession:
        if session.state != RemoteControlState.ISSUED:
            raise RemoteControlError(
                f"accept inválido desde {session.state}"
            )
        now = self.clock()
        if now > session.token_expires_at:
            return self._mark_ended(
                session, RemoteControlEndReason.TIMEOUT, now
            )
        return _replace(session, state=RemoteControlState.ACCEPTED, accepted_at=now)

    def activate(
        self, session: RemoteControlSession
    ) -> RemoteControlSession:
        if session.state != RemoteControlState.ACCEPTED:
            raise RemoteControlError(
                f"activate inválido desde {session.state}"
            )
        return _replace(session, state=RemoteControlState.ACTIVE)

    def end(
        self,
        session: RemoteControlSession,
        reason: RemoteControlEndReason,
    ) -> RemoteControlSession:
        return self._mark_ended(session, reason, self.clock())

    def _mark_ended(
        self,
        session: RemoteControlSession,
        reason: RemoteControlEndReason,
        now: datetime,
    ) -> RemoteControlSession:
        if session.state == RemoteControlState.ENDED:
            return session
        return _replace(
            session,
            state=RemoteControlState.ENDED,
            ended_at=now,
            end_reason=reason,
        )


def _replace(session: RemoteControlSession, **kwargs) -> RemoteControlSession:
    """dataclass replace para frozen dataclass."""
    from dataclasses import replace

    return replace(session, **kwargs)
