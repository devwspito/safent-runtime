"""WorkspaceState — state machine del ciclo de vida del Workspace (T073).

FR-001..FR-005. Dominio puro, sin framework.

Ciclo de vida:
    PROVISIONING → ACTIVE → SUSPENDED → ACTIVE (ciclo)
    Cualquier estado no terminal → CLOSED
    ACTIVE → CRASHED → CLOSED
"""

from __future__ import annotations

from enum import StrEnum


class WorkspaceState(StrEnum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"
    CRASHED = "crashed"


_ALLOWED: dict[WorkspaceState, frozenset[WorkspaceState]] = {
    WorkspaceState.PROVISIONING: frozenset(
        {WorkspaceState.ACTIVE, WorkspaceState.CLOSED, WorkspaceState.CRASHED}
    ),
    WorkspaceState.ACTIVE: frozenset(
        {WorkspaceState.SUSPENDED, WorkspaceState.CLOSED, WorkspaceState.CRASHED}
    ),
    WorkspaceState.SUSPENDED: frozenset(
        {WorkspaceState.ACTIVE, WorkspaceState.CLOSED}
    ),
    WorkspaceState.CLOSED: frozenset(),
    WorkspaceState.CRASHED: frozenset({WorkspaceState.CLOSED}),
}

_TERMINAL: frozenset[WorkspaceState] = frozenset(
    {WorkspaceState.CLOSED}
)


class WorkspaceStateTransitionError(RuntimeError):
    """Transición de WorkspaceState no permitida."""


def assert_transition(
    current: WorkspaceState, target: WorkspaceState
) -> None:
    """Lanza WorkspaceStateTransitionError si la transición no está permitida."""
    if target not in _ALLOWED[current]:
        raise WorkspaceStateTransitionError(
            f"Transición no permitida: {current} → {target}. "
            f"Permitidas desde {current}: {sorted(_ALLOWED[current])}"
        )


def is_terminal(state: WorkspaceState) -> bool:
    """True si el estado es terminal (no admite más transiciones)."""
    return state in _TERMINAL
