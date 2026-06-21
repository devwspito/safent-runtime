"""FakeCapabilityBroker — fake de CapabilityBrokerPort para tests unitarios.

Permite scriptear ExecutionOutcome por propuesta o por defecto.
Registra todos los dispatch para assertions en tests.
"""

from __future__ import annotations

from uuid import UUID

from hermes.capabilities.domain.ports import (
    CapabilityBrokerPort,
    ConsentContext,
    ExecutionOutcome,
    ExecutionStatus,
)
from hermes.domain.proposal import ToolCallProposal


class FakeCapabilityBroker:
    """Fake scriptable de CapabilityBrokerPort.

    Args:
        default_outcome: ExecutionOutcome devuelto si no hay scripted específico
            para el proposal_id. Por defecto devuelve EXECUTED con audit_entry_id.
        scripted: mapeo proposal_id -> ExecutionOutcome para control fino.
    """

    def __init__(
        self,
        *,
        default_outcome: ExecutionOutcome | None = None,
        scripted: dict[UUID, ExecutionOutcome] | None = None,
    ) -> None:
        self._default = default_outcome
        self._scripted: dict[UUID, ExecutionOutcome] = scripted or {}
        self.dispatched: list[tuple[ToolCallProposal, ConsentContext]] = []

    def script(self, proposal_id: UUID, outcome: ExecutionOutcome) -> None:
        """Registra un outcome para un proposal_id específico."""
        self._scripted[proposal_id] = outcome

    async def dispatch(
        self,
        proposal: ToolCallProposal,
        consent_context: ConsentContext,
        *,
        hitl_approval_token: str | None = None,  # noqa: ARG002
        work_item_id: UUID | None = None,  # noqa: ARG002
        autonomy_level: object | None = None,  # noqa: ARG002
    ) -> ExecutionOutcome:
        self.dispatched.append((proposal, consent_context))
        if proposal.proposal_id in self._scripted:
            return self._scripted[proposal.proposal_id]
        if self._default is not None:
            return self._default
        # Default: EXECUTED con audit_entry_id real
        from uuid import uuid4  # noqa: PLC0415
        return ExecutionOutcome(
            proposal_id=proposal.proposal_id,
            status=ExecutionStatus.EXECUTED,
            audit_entry_id=uuid4(),
        )


# Satisface CapabilityBrokerPort structural check
assert isinstance(FakeCapabilityBroker(), CapabilityBrokerPort)
