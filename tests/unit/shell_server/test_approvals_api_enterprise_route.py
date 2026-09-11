"""approvals_api — Enterprise-routed row surfacing (Fase 2 Phase 4b).

Local APPROVE on a route='enterprise' pending row must surface as 403
Forbidden with a clear "pendiente de aprobación de tu empresa" message (the
gate rejects it fail-closed, see sqlite_approval_gate.approve()'s guard) — NOT
as a generic 400/401. Local DENY must be completely unaffected (I-2).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.capabilities.infrastructure.sqlite_approval_gate import ApprovalGateError
from hermes.shell_server.cowork.approvals_api import create_approvals_router


pytestmark = pytest.mark.unit


def _make_client(control_plane) -> TestClient:
    app = FastAPI()
    app.state.control_plane = control_plane
    app.include_router(create_approvals_router())
    return TestClient(app, raise_server_exceptions=True)


class _FakeControlPlaneApproveRaises:
    async def approve(self, *, channel, proposal_id):
        del channel  # Interface inputs intentionally unused by this denial stub.
        raise ApprovalGateError(
            f"proposal_id={proposal_id} está enrutada a Enterprise.",
            reason="enterprise_route_requires_cloud_decision",
        )


class _FakeControlPlaneRejectOk:
    async def reject(self, *, channel, proposal_id, reason):
        del channel, proposal_id, reason  # Interface-only inputs for the successful denial stub.


class TestEnterpriseRouteApproveRejectedWith403:
    def test_approve_on_enterprise_row_returns_403_with_clear_message(self) -> None:
        client = _make_client(_FakeControlPlaneApproveRaises())
        pid = str(uuid4())

        resp = client.post(
            f"/api/v1/approvals/{pid}", json={"decision": "once"}
        )

        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["code"] == "enterprise_route_requires_cloud_decision"
        assert "empresa" in body["detail"]["message"].lower()


class TestEnterpriseRouteDenyStillWorks:
    def test_deny_on_enterprise_row_is_unaffected(self) -> None:
        """I-2: DENY never goes through approve()'s enterprise-route guard —
        it must succeed exactly like on any other row."""
        client = _make_client(_FakeControlPlaneRejectOk())
        pid = str(uuid4())

        resp = client.post(
            f"/api/v1/approvals/{pid}", json={"decision": "deny"}
        )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "decision": "deny"}


class TestPendingListSurfacesRoute:
    def test_gate_does_not_advertise_unsupported_permanent_approval(self) -> None:
        client = _make_client(_FakeControlPlaneApproveRaises())
        response = client.post(
            f"/api/v1/approvals/{uuid4()}", json={"decision": "always"}
        )
        assert response.status_code == 422

    def test_unavailable_gate_is_not_reported_as_an_empty_queue(self) -> None:
        class Unavailable:
            async def list_hitl_pending(self):
                raise RuntimeError("internal details must not reach the response")

        response = _make_client(Unavailable()).get("/api/v1/approvals/pending")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "approvals_unavailable"
        assert "internal details" not in response.text

    def test_to_frontend_surfaces_enterprise_route(self) -> None:
        from hermes.shell_server.cowork.approvals_api import _to_frontend

        row = {
            "proposal_id": str(uuid4()),
            "tool_name": "cronjob",
            "risk": "high",
            "justification": "j",
            "parameters_redacted": {},
            "route": "enterprise",
        }
        result = _to_frontend(row)
        assert result["route"] == "enterprise"

    def test_to_frontend_defaults_route_to_local(self) -> None:
        from hermes.shell_server.cowork.approvals_api import _to_frontend

        row = {
            "proposal_id": str(uuid4()),
            "tool_name": "cronjob",
            "risk": "high",
            "justification": "j",
            "parameters_redacted": {},
        }
        result = _to_frontend(row)
        assert result["route"] == "local"
