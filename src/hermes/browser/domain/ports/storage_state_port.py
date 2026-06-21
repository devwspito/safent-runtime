"""StorageStatePort: persistencia cifrada de la sesion del cliente.

Cada sesion exitosa persiste cookies + localStorage + sessionStorage
del browser context, cifrado at-rest, scopado por (tenant_id, site_id).
Sesiones concurrentes sobre el mismo par son imposibles por contrato (lock).

Contratos copiados del spec ``contracts/storage_state_port.py`` y adaptados
para ser el import estable de produccion.

Threat-model control P1 #1 — surface 2 (StorageState cross-tenant replay).
Constitution IV: fail-closed; InvalidTag propagates → StorageStateCorrupt.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StorageStateError(RuntimeError):
    """Base de errores del puerto."""


class StorageStateCorrupt(StorageStateError):
    """Blob no descifrable o estructura invalida.

    Raised when AES-GCM ``InvalidTag`` is caught during decrypt.
    Caller must fall to clean state + request reauth (constitution IV).
    """


class StorageStateLocked(StorageStateError):
    """Otro proceso retiene el lock para (tenant_id, site_id).

    Raised when lock() times out (timeout_s exceeded).
    """


class OperatorReauthRequired(RuntimeError):
    """Remote site session has expired.

    Raised by BrowserSession._execute when expiration_detector returns True.
    The caller must request reauth from the operator; it must NOT retry with
    stored credentials (FR-005, SC-006, constitution II).
    """

    def __init__(self, reason: str = "EXPIRED_REMOTE") -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class StorageStateInvalidationReason(StrEnum):
    EXPIRED_REMOTE = "expired_remote"
    USER_LOGGED_OUT = "user_logged_out"
    REAUTH_REQUIRED = "reauth_required"
    CORRUPT_DECRYPT = "corrupt_decrypt"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class EncryptedStorageState:
    """Blob cifrado opaco por (tenant_id, site_id).

    ``ciphertext``: AES-GCM-256 over Playwright native JSON storage_state
    (cookies + origins).  AAD binds ciphertext to (tenant_id, site_id, kid).
    ``kid``: key identifier in the consumer KMS.
    ``alg``: algorithm name; default 'AES-GCM-256'.
    """

    tenant_id: UUID
    site_id: str
    ciphertext: bytes
    kid: str
    alg: str = "AES-GCM-256"
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    invalidated_at: datetime | None = None
    invalidation_reason: StorageStateInvalidationReason | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StorageStatePort(Protocol):
    """Contrato de persistencia cifrada del StorageState.

    Implementaciones esperadas:
      - InMemoryStorageStatePort (tests).
      - PostgresStorageStatePort (gestoria-agent + adapter cifrado).

    REQUISITO de adapter: save() DEBE ocurrir dentro del lock() del caller.
    Un save fuera del lock es una violacion del contrato (threat-model R1).
    """

    async def load(
        self, *, tenant_id: UUID, site_id: str
    ) -> EncryptedStorageState | None:
        """Devuelve el ultimo blob no-invalidado, o None.

        Does not decrypt.  The caller (BrowserSession + crypto adapter)
        decrypts before attaching to the browser context.
        """
        ...

    async def save(self, state: EncryptedStorageState) -> None:
        """Persiste o reemplaza el blob para (tenant_id, site_id).

        Upsert: if an active blob exists, replaces it atomically.
        Anti-rebase: adapter must guarantee this runs inside lock().
        Raises StorageStateLocked if called outside the current lock context.
        """
        ...

    async def invalidate(
        self,
        *,
        tenant_id: UUID,
        site_id: str,
        reason: StorageStateInvalidationReason,
    ) -> None:
        """Marca como invalidated. load() siguiente devuelve None.

        Idempotent: calling twice does not raise.
        Audit log with reason is mandatory (threat-model R1).
        """
        ...

    def lock(
        self, *, tenant_id: UUID, site_id: str, timeout_s: float = 30.0
    ) -> AbstractAsyncContextManager[None]:
        """Serializa acceso por (tenant_id, site_id) (FR-006).

        Adapter Postgres: pg_advisory_xact_lock(hashtext(...)).
        Adapter in-memory: asyncio.Lock por par.

        If not acquired within timeout_s → raises StorageStateLocked.
        """
        ...


__all__ = [
    "EncryptedStorageState",
    "OperatorReauthRequired",
    "StorageStateCorrupt",
    "StorageStateError",
    "StorageStateInvalidationReason",
    "StorageStateLocked",
    "StorageStatePort",
]
