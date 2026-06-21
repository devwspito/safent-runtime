"""InMemoryStorageStatePort: test double del StorageStatePort.

Implementa el Protocol con asyncio.Lock por par (tenant_id, site_id).
Enforce: save() solo puede llamarse desde dentro del lock() activo.
Si se llama fuera → StorageStateLocked.

Constitution V: sin Postgres, sin red. Apto para tests/unit/.
Threat-model R1 (surface 2): save outside lock → StorageStateLocked.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from uuid import UUID

from hermes.browser.domain.ports.storage_state_port import (
    EncryptedStorageState,
    StorageStateInvalidationReason,
    StorageStateLocked,
)

logger = logging.getLogger(__name__)

# Context key: (tenant_id, site_id) → True if caller holds the lock.
_LockKey = tuple[UUID, str]


class InMemoryStorageStatePort:
    """StorageStatePort respaldado en memoria. Para tests unitarios."""

    def __init__(self) -> None:
        # (tenant_id, site_id) → EncryptedStorageState | None
        self._store: dict[_LockKey, EncryptedStorageState] = {}
        # One asyncio.Lock per (tenant_id, site_id).
        self._locks: dict[_LockKey, asyncio.Lock] = {}
        # Track which keys are currently locked by caller.
        self._locked_keys: set[_LockKey] = set()

    # ------------------------------------------------------------------
    # StorageStatePort implementation
    # ------------------------------------------------------------------

    async def load(
        self, *, tenant_id: UUID, site_id: str
    ) -> EncryptedStorageState | None:
        key: _LockKey = (tenant_id, site_id)
        state = self._store.get(key)
        if state is None or state.invalidated_at is not None:
            return None
        return state

    async def save(self, state: EncryptedStorageState) -> None:
        key: _LockKey = (state.tenant_id, state.site_id)
        if key not in self._locked_keys:
            raise StorageStateLocked(
                f"save() called outside lock() for {key} — "
                "anti-rebase contract violation (threat-model R1)"
            )
        self._store[key] = state
        logger.info(
            "hermes.browser.storage_state.saved",
            extra={
                "tenant_id": str(state.tenant_id),
                "site_id": state.site_id,
                "kid": state.kid,
            },
        )

    async def invalidate(
        self,
        *,
        tenant_id: UUID,
        site_id: str,
        reason: StorageStateInvalidationReason,
    ) -> None:
        key: _LockKey = (tenant_id, site_id)
        existing = self._store.get(key)
        if existing is None:
            # Idempotent: nothing to invalidate.
            logger.debug(
                "hermes.browser.storage_state.invalidate_noop",
                extra={"tenant_id": str(tenant_id), "site_id": site_id, "reason": reason},
            )
            return
        if existing.invalidated_at is not None:
            # Already invalidated — idempotent.
            logger.debug(
                "hermes.browser.storage_state.already_invalidated",
                extra={"tenant_id": str(tenant_id), "site_id": site_id, "reason": reason},
            )
            return
        # Replace with invalidated copy (frozen dataclass).
        self._store[key] = EncryptedStorageState(
            tenant_id=existing.tenant_id,
            site_id=existing.site_id,
            ciphertext=existing.ciphertext,
            kid=existing.kid,
            alg=existing.alg,
            updated_at=existing.updated_at,
            invalidated_at=datetime.now(tz=UTC),
            invalidation_reason=reason,
        )
        logger.info(
            "hermes.browser.storage_state.invalidated",
            extra={
                "tenant_id": str(tenant_id),
                "site_id": site_id,
                # Do not log kid in user-visible errors; ok here in audit.
                "reason": reason,
            },
        )

    def lock(
        self, *, tenant_id: UUID, site_id: str, timeout_s: float = 30.0
    ) -> AbstractAsyncContextManager[None]:
        key: _LockKey = (tenant_id, site_id)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return _AsyncLockContext(
            lock=self._locks[key],
            key=key,
            locked_keys=self._locked_keys,
            timeout_s=timeout_s,
        )


# ---------------------------------------------------------------------------
# Lock context manager
# ---------------------------------------------------------------------------


class _AsyncLockContext:
    """Async context manager wrapping asyncio.Lock with timeout."""

    def __init__(
        self,
        *,
        lock: asyncio.Lock,
        key: _LockKey,
        locked_keys: set[_LockKey],
        timeout_s: float,
    ) -> None:
        self._lock = lock
        self._key = key
        self._locked_keys = locked_keys
        self._timeout_s = timeout_s

    async def __aenter__(self) -> None:
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self._timeout_s)
        except TimeoutError:
            raise StorageStateLocked(
                f"Could not acquire lock for {self._key} within {self._timeout_s}s"
            ) from None
        self._locked_keys.add(self._key)

    async def __aexit__(self, *_: object) -> None:
        self._locked_keys.discard(self._key)
        if self._lock.locked():
            self._lock.release()
