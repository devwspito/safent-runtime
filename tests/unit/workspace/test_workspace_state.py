"""Tests del state machine WorkspaceState (T073, FR-001..FR-005)."""
from __future__ import annotations

import pytest

from hermes.workspace.domain.workspace_state import (
    WorkspaceState,
    WorkspaceStateTransitionError,
    assert_transition,
    is_terminal,
)

pytestmark = pytest.mark.unit


class TestTransitions:
    def test_provisioning_to_active(self) -> None:
        assert_transition(WorkspaceState.PROVISIONING, WorkspaceState.ACTIVE)

    def test_active_to_suspended(self) -> None:
        assert_transition(WorkspaceState.ACTIVE, WorkspaceState.SUSPENDED)

    def test_suspended_to_active(self) -> None:
        assert_transition(WorkspaceState.SUSPENDED, WorkspaceState.ACTIVE)

    def test_any_to_closed_or_crashed(self) -> None:
        for src in (
            WorkspaceState.PROVISIONING,
            WorkspaceState.ACTIVE,
            WorkspaceState.SUSPENDED,
        ):
            assert_transition(src, WorkspaceState.CLOSED)

    def test_closed_is_terminal(self) -> None:
        assert is_terminal(WorkspaceState.CLOSED)
        for tgt in (WorkspaceState.ACTIVE, WorkspaceState.SUSPENDED):
            with pytest.raises(WorkspaceStateTransitionError):
                assert_transition(WorkspaceState.CLOSED, tgt)

    def test_crashed_only_to_closed(self) -> None:
        assert_transition(WorkspaceState.CRASHED, WorkspaceState.CLOSED)
        with pytest.raises(WorkspaceStateTransitionError):
            assert_transition(WorkspaceState.CRASHED, WorkspaceState.ACTIVE)

    def test_suspended_cannot_crash_directly(self) -> None:
        with pytest.raises(WorkspaceStateTransitionError):
            assert_transition(WorkspaceState.SUSPENDED, WorkspaceState.CRASHED)
