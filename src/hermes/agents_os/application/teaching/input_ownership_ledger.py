"""InputOwnershipLedger — in-memory, fail-closed poseedor único (FR-002/FR-022).

One ledger per process. Thread-safe via RLock.

Invariant: at most ONE owner per context_id at any time.
Attempting to claim an already-claimed context with a DIFFERENT owner raises
InputOwnershipViolation immediately (fail-closed, constitución IV).

Idempotency: claiming with the SAME owner that already holds the context is
a no-op (safe for retry paths).
"""

from __future__ import annotations

import threading
from uuid import UUID

from hermes.agents_os.application.teaching.teaching_context import (
    InputOwner,
    InputOwnershipViolation,
)


class InputOwnershipLedger:
    """In-memory registry of context_id → current InputOwner.

    All operations are O(1) and protected by an RLock so concurrent
    teaching-session opens cannot race past the claim gate.
    """

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
                    f"cannot claim for {owner!r} (FR-002 fail-closed)."
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
