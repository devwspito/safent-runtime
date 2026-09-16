"""InputOwnershipLedger — in-memory, fail-closed sole-owner registry.

Split out of the retired teach-by-browser feature's
``agents_os/application/teaching/`` package (retired 10-sep-2026, see
specs/025-safent-repaso/retirada-ensenar.md) into a minimal, teaching-free
type: ``SessionInputBridge`` (the jailed session's input server) uses it as
a contention guard so agent input is refused while a human mirror session
holds the input channel — a safety invariant independent of teaching.

One ledger per process. Thread-safe via RLock.

Invariant: at most ONE owner per context_id at any time. Attempting to claim
an already-claimed context with a DIFFERENT owner raises
InputOwnershipViolation immediately (fail-closed).

Idempotency: claiming with the SAME owner that already holds the context is
a no-op (safe for retry paths).
"""

from __future__ import annotations

import threading
from enum import StrEnum
from uuid import UUID


class InputOwner(StrEnum):
    """Who holds the input channel of a context."""

    AGENT = "agent"
    OPERATOR = "operator"


class InputOwnershipViolation(RuntimeError):
    """Raised when input ownership invariant would be broken."""


class InputOwnershipLedger:
    """In-memory registry of context_id → current InputOwner."""

    def __init__(self) -> None:
        self._owners: dict[UUID, InputOwner] = {}
        self._lock = threading.RLock()

    def claim(self, context_id: UUID, owner: InputOwner) -> None:
        """Claim ownership of *context_id* for *owner*.

        Idempotent when called repeatedly by the same owner.

        Raises:
            InputOwnershipViolation: if another owner already holds the context.
        """
        with self._lock:
            current = self._owners.get(context_id)
            if current is None:
                self._owners[context_id] = owner
                return
            if current != owner:
                raise InputOwnershipViolation(
                    f"Context {context_id} is already owned by {current!r}; "
                    f"cannot claim for {owner!r} (fail-closed)."
                )
            # Same owner — idempotent, no-op.

    def owner_of(self, context_id: UUID) -> InputOwner | None:
        """Return the current owner, or None if unclaimed."""
        with self._lock:
            return self._owners.get(context_id)

    def release(self, context_id: UUID) -> None:
        """Release ownership, making the context available for re-claim.

        Silently no-ops for unknown context_ids (safe for cleanup paths).
        """
        with self._lock:
            self._owners.pop(context_id, None)
