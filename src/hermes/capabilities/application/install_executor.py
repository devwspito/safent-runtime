"""InstallExecutorPort — port (Protocol) for the install executor.

The install executor handles search + install + connect_integration tool calls
that route through the broker with executor="install".  It sits in the
application layer as a port so the infrastructure adapter (Dbus*) never
leaks into the broker.

All implementations MUST be fail-closed:
  - Blocked scan   → ReplayStatus.REJECTED_BY_POLICY
  - ok=False       → ReplayStatus.EXECUTED_FAILED
  - ok=True        → ReplayStatus.EXECUTED_OK

The broker owns the gate sequence (kill-switch → consent → HITL → audit).
This port executes ONLY after the broker has passed every gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from hermes.agents_os.domain.ports.surface_adapter_port import (
        CapturedAction,
        ReplayOutcome,
    )
    from hermes.domain.proposal import ToolCallProposal


@runtime_checkable
class InstallExecutorPort(Protocol):
    """Executes install/search/connect proposals after broker gate-sequence.

    Each method receives the broker-validated proposal and the synthesised
    CapturedAction.  The implementation maps tool_name → the appropriate
    wiring function, passing the resolved owner uid so authZ + scan run on the
    existing wiring path unchanged.
    """

    async def execute(
        self,
        proposal: ToolCallProposal,
        action: CapturedAction,
    ) -> ReplayOutcome:
        """Map proposal.tool_name to the wiring function and execute it.

        Returns:
            ReplayOutcome with EXECUTED_OK, EXECUTED_FAILED, or
            REJECTED_BY_POLICY — never raises.
        """
        ...
