"""FakeApprovalGate — fake de ApprovalGatePort para tests unitarios."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from hermes.capabilities.domain.ports import (
    ApprovalGatePort,
    ConsentContext,
    RiskLevel,
)


class FakeApprovalGate:
    """Fake de ApprovalGatePort. Por defecto auto-aprueba (útil para E2E tests).

    Args:
        auto_approve: si True, approved_token_for devuelve un token sin esperar
            llamada explícita a approve(). Default False.
    """

    def __init__(self, *, auto_approve: bool = False) -> None:
        self._auto_approve = auto_approve
        self._pending: dict[UUID, dict[str, Any]] = {}
        self._approved: dict[UUID, str] = {}
        self._rejected: set[UUID] = set()
        self.register_calls: list[UUID] = []
        self.approve_calls: list[UUID] = []
        self.reject_calls: list[UUID] = []

    async def register_pending(
        self,
        *,
        proposal_id: UUID,
        work_item_id: UUID,
        consent_context: ConsentContext,  # noqa: ARG002
        risk: RiskLevel,
        justification: str,
        parameters_redacted: dict[str, Any],  # noqa: ARG002
        tool_name: str = "",
        action_digest: str = "",  # noqa: ARG002
        conversation_id: str = "",
    ) -> str:
        self.register_calls.append(proposal_id)
        self._pending[proposal_id] = {
            "work_item_id": work_item_id,
            "risk": risk,
            "justification": justification,
            "tool_name": tool_name,
            "conversation_id": conversation_id,
        }
        if self._auto_approve:
            token = str(uuid4())
            self._approved[proposal_id] = token
        return "pending"

    async def verify_token(self, *, proposal_id: UUID, token: str) -> bool:
        return self._approved.get(proposal_id) == token

    async def approved_token_for(self, proposal_id: UUID) -> str | None:
        return self._approved.get(proposal_id)

    async def approve(
        self, *, proposal_id: UUID, approved_by: UUID  # noqa: ARG002
    ) -> str:
        self.approve_calls.append(proposal_id)
        token = str(uuid4())
        self._approved[proposal_id] = token
        return token

    async def work_item_id_for_proposal(self, proposal_id: UUID) -> UUID | None:
        entry = self._pending.get(proposal_id)
        if entry is None:
            return None
        work_item_id = entry.get("work_item_id")
        if work_item_id is None:
            return None
        return work_item_id if isinstance(work_item_id, UUID) else UUID(str(work_item_id))

    async def reject(
        self,
        *,
        proposal_id: UUID,
        rejected_by: UUID,  # noqa: ARG002
        reason: str,  # noqa: ARG002
    ) -> None:
        self.reject_calls.append(proposal_id)
        self._rejected.add(proposal_id)


# Satisface ApprovalGatePort structural check
assert isinstance(FakeApprovalGate(), ApprovalGatePort)
