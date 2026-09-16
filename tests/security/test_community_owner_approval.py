"""Community has no MFA API/factors; owner identity and action tokens remain."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
from hermes.agents_os.infrastructure.dbus_runtime_service import DbusRuntimeServiceWiring
from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
from hermes.capabilities.domain.ports import ConsentContext, RiskLevel
from hermes.capabilities.infrastructure.sqlite_approval_gate import (
    ApprovalGateError,
    SqliteApprovalGate,
)
from hermes.shell_server.cowork.approvals_api import _to_frontend, create_approvals_router
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit


@pytest.fixture
def gate(tmp_path):
    return SqliteApprovalGate(
        db_path=tmp_path / "state.db",
        minter=HitlApprovalMinter(signing_key=b"k" * 32),
        signer=AuditHashChainSigner(signing_key=b"k" * 32),
    )


async def register(gate):
    pid = uuid4()
    await gate.register_pending(
        proposal_id=pid,
        work_item_id=uuid4(),
        consent_context=ConsentContext(tenant_id=uuid4(), operator_id=uuid4()),
        risk=RiskLevel.HIGH,
        justification="owner decision",
        parameters_redacted={"name": "example"},
        tool_name="install_app",
    )
    return pid


async def test_owner_approval_still_has_single_use_action_token(gate):
    pid = await register(gate)
    token = await gate.approve(proposal_id=pid, approved_by=uuid4())
    assert not await gate.verify_token(proposal_id=uuid4(), token=token)
    assert await gate.verify_token(proposal_id=pid, token=token)
    assert not await gate.verify_token(proposal_id=pid, token=token)
    with pytest.raises(ApprovalGateError):
        await gate.approve(proposal_id=pid, approved_by=uuid4())


async def test_untrusted_uid_cannot_approve_without_mfa(gate):
    from hermes.agents_os.infrastructure.dbus_runtime_service import DbusAuthorizationError

    pid = await register(gate)
    wiring = DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(paused=False),
        approval_gate=gate,
        authorized_uids=frozenset({1000}),
    )
    with pytest.raises(DbusAuthorizationError):
        await wiring.approve_action(proposal_id=pid, sender_uid=2000)
    assert await gate.approved_token_for(pid) is None


@pytest.fixture
def client():
    app = FastAPI()
    app.state.control_plane = AsyncMock()
    app.include_router(create_approvals_router())
    return TestClient(app)


@pytest.mark.parametrize(
    "field,value", [("totp", "123456"), ("mfa_factors", {}), ("approved_by", str(uuid4()))]
)
def test_approval_api_does_not_accept_factor_or_identity_payload(client, field, value):
    response = client.post(f"/api/v1/approvals/{uuid4()}", json={"decision": "once", field: value})
    assert response.status_code == 422
    client.app.state.control_plane.approve.assert_not_called()


@pytest.mark.parametrize(
    "method,path",
    [("get", "/api/v1/mfa/status"), ("post", "/api/v1/mfa/enroll"), ("post", "/api/v1/mfa/riddle")],
)
def test_community_has_no_mfa_routes(client, method, path):
    assert getattr(client, method)(path).status_code == 404


def test_high_risk_pending_row_does_not_request_totp():
    row = _to_frontend({"tool_name": "install_app", "risk": "high", "route": "enterprise"})
    assert row["required_level"] == "simple"
    assert row["route"] == "enterprise"
