"""ExecutionContextRegistry — registro in-memory fail-closed de propiedad de superficies.

T062 (CTRL-P1-18, FR-021..FR-023, FR-026, NFR-004).

Generaliza `InputOwnershipLedger` de teaching
(`agents_os/application/teaching/input_ownership_ledger.py`) a un registro con
clave `InputSurfaceKey` (surface kind + surface_id) en vez de `context_id` UUID.

Invariantes (idénticos a los del ledger de teaching, escalados a superficies):
  - A lo sumo UN dueño por InputSurfaceKey en todo momento (FR-021).
  - Claim por un SEGUNDO owner → InputOwnershipViolation inmediata (FR-022,
    fail-closed, constitución IV/NFR-004).
  - Claim por el MISMO owner es idempotente (retry-safe).
  - release() y release_all_for() son cleanup-safe (no lanzan si ya libre,
    FR-023).
  - reconcile() limpia TODO al arranque del daemon (FR-026/SC-010); ningún
    dueño del proceso anterior sobrevive.

Thread/worker-safety: RLock (un solo proceso asyncio, workers = coroutines).
Spec 004 inalterado: `InputOwnershipLedger` de teaching no se modifica.

Modelo híbrido (data-model 006 §3.2):
  - El lock RLock en memoria es el camino rápido (O(1), sub-ms).
  - El `store` SQLite es la durabilidad (write-through) + la red de seguridad
    (UNIQUE parcial) para reconciliar tras reinicio.
  - `store` es OPCIONAL: si se omite, el registry funciona en memoria pura
    (útil para unit-tests y para el ledger de teaching que no persiste).
"""

from __future__ import annotations

import threading
from uuid import uuid4

from hermes.execution.domain.ports import (
    ExecutionContextId,
    InputOwnerKind,
    InputOwnershipViolation,
    InputSurfaceKey,
)

try:
    from hermes.execution.infrastructure.sqlite_execution_context_store import (
        SqliteExecutionContextStore,
    )
    _STORE_TYPE = SqliteExecutionContextStore
except ImportError:  # pragma: no cover — store is optional
    _STORE_TYPE = None  # type: ignore[assignment]


_DEFAULT_LEASE_S = 300.0  # seconds; workers renew via heartbeat


class ExecutionContextRegistry:
    """In-memory registry of InputSurfaceKey → current owner ExecutionContextId.

    One registry per daemon process. RLock-protected so that concurrent asyncio
    tasks (workers) cannot race past the claim gate.

    Optionally backed by a SqliteExecutionContextStore for write-through
    durability (T063). When store=None the registry is pure in-memory (spec 004
    teaching ledger pattern, no persistence).
    """

    def __init__(self, *, store: SqliteExecutionContextStore | None = None) -> None:
        # surface_key → (owner, context_id for the DB row)
        self._owners: dict[InputSurfaceKey, tuple[ExecutionContextId, str]] = {}
        self._lock = threading.RLock()
        self._store = store

    # ------------------------------------------------------------------
    # claim / owner_of / release (core invariant — FR-021/FR-022/FR-023)
    # ------------------------------------------------------------------

    def claim(
        self,
        *,
        surface: InputSurfaceKey,
        owner: ExecutionContextId,
        isolation_key: str | None = None,
        lease_seconds: float = _DEFAULT_LEASE_S,
    ) -> str:
        """Claim *surface* for *owner*. Idempotent with the SAME owner.

        Returns the context_id (UUID str) for this claim row (used by store).

        Raises:
            InputOwnershipViolation: if another owner already holds the surface.
        """
        with self._lock:
            current_entry = self._owners.get(surface)
            if current_entry is not None:
                current_owner, existing_ctx_id = current_entry
                if current_owner != owner:
                    raise InputOwnershipViolation(
                        f"Surface {surface!r} already owned by {current_owner!r}; "
                        f"cannot claim for {owner!r} (FR-022 fail-closed)."
                    )
                # Same owner — idempotent, return existing context_id.
                return existing_ctx_id

            ctx_id = str(uuid4())
            self._owners[surface] = (owner, ctx_id)

        # Write-through outside the lock (disk I/O; lock already released).
        if self._store is not None:
            eff_isolation_key = isolation_key or _surface_isolation_key(surface)
            self._store.write_claim(
                context_id=ctx_id,
                input_surface=surface.kind.value,
                isolation_key=eff_isolation_key,
                input_owner=_owner_kind_to_db(owner.owner_kind),
                owning_worker_id=str(owner.value),
                lease_seconds=lease_seconds,
            )

        return ctx_id

    def owner_of(self, *, surface: InputSurfaceKey) -> ExecutionContextId | None:
        """Return the current owner, or None if the surface is free."""
        with self._lock:
            entry = self._owners.get(surface)
            return entry[0] if entry is not None else None

    def release(self, *, surface: InputSurfaceKey) -> None:
        """Release the surface (re-claimable). No-op if already free (FR-023)."""
        with self._lock:
            entry = self._owners.pop(surface, None)

        if entry is not None and self._store is not None:
            _, ctx_id = entry
            self._store.write_release(context_id=ctx_id)

    # ------------------------------------------------------------------
    # release_all_for — worker termination without leaks (FR-023)
    # ------------------------------------------------------------------

    def release_all_for(self, *, owner: ExecutionContextId) -> int:
        """Release ALL surfaces held by *owner*. Returns count released."""
        with self._lock:
            to_release = [
                (surf, ctx_id)
                for surf, (own, ctx_id) in self._owners.items()
                if own == owner
            ]
            for surf, _ in to_release:
                del self._owners[surf]

        if self._store is not None:
            for _, ctx_id in to_release:
                self._store.write_release(context_id=ctx_id)

        return len(to_release)

    # ------------------------------------------------------------------
    # reconcile — daemon restart cleanup (FR-026, SC-010)
    # ------------------------------------------------------------------

    def reconcile(self) -> int:
        """Clear ALL owners in memory (daemon restart — FR-026).

        After a daemon restart the in-memory state is gone. This call models
        that: every surface that was 'owned' by the previous process is now
        an orphan and must be freed.

        Also persists the release to the store (if configured) so that
        bootstrap can assert 0 orphaned rows in the DB.

        Returns the number of entries purged.
        """
        with self._lock:
            purged = list(self._owners.items())
            self._owners.clear()

        count = len(purged)

        if self._store is not None and count > 0:
            self._store.reconcile_all_claimed()

        return count


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------


def _surface_isolation_key(surface: InputSurfaceKey) -> str:
    """Derive isolation_key from InputSurfaceKey (injective, deterministic)."""
    return f"{surface.kind.value}:{surface.surface_id}"


def _owner_kind_to_db(kind: InputOwnerKind) -> str:
    """Map InputOwnerKind to the 'agent'|'operator' DB enum (§8 point 7)."""
    return "agent" if kind == InputOwnerKind.AGENT_TASK else "operator"
