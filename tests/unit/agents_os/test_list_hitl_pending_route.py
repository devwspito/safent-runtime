"""DbusRuntimeServiceWiring.list_hitl_pending — surfaces `route` (Fase 2 Phase 4b).

Read-only supervision method: a route='enterprise' row must surface
route="enterprise" (so the web UI can show "pendiente de aprobación de tu
empresa" and disable local Approve); every other row (LOCAL, or a row
registered before this phase) defaults to route="local".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import DbusRuntimeServiceWiring
from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
from hermes.capabilities.domain.ports import ConsentContext, RiskLevel
from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit


def _make_gate(db_path: Path) -> SqliteApprovalGate:
    signer = MagicMock()
    signer.append = MagicMock()
    signer.append_and_persist = AsyncMock()
    return SqliteApprovalGate(
        db_path=db_path,
        minter=HitlApprovalMinter(signing_key=b"k" * 32),
        signer=signer,
        audit_repo=None,
        mfa_verifier=None,
    )


@pytest.mark.asyncio
async def test_list_hitl_pending_surfaces_route_for_each_row(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    gate = _make_gate(db_path)

    tenant_id, operator_id = uuid4(), uuid4()
    await gate.register_pending(
        proposal_id=uuid4(),
        work_item_id=uuid4(),
        consent_context=ConsentContext(tenant_id=tenant_id, operator_id=operator_id),
        risk=RiskLevel.HIGH,
        justification="local row",
        parameters_redacted={},
        tool_name="send_message",
        action_digest="dig-local",
    )
    await gate.register_pending(
        proposal_id=uuid4(),
        work_item_id=uuid4(),
        consent_context=ConsentContext(tenant_id=tenant_id, operator_id=operator_id),
        risk=RiskLevel.HIGH,
        justification="enterprise row",
        parameters_redacted={},
        tool_name="cronjob",
        action_digest="dig-enterprise",
        route="enterprise",
        agent_id="agent-a",
    )

    wiring = DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=gate,
        authorized_uids=frozenset({1000}),
    )

    pending = await wiring.list_hitl_pending()
    routes = {row["tool_name"]: row["route"] for row in pending}

    assert routes["send_message"] == "local"
    assert routes["cronjob"] == "enterprise"
