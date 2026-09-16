"""Regression test — specs/025-safent-repaso hallazgo #3
("GET /agents/active -> 405").

Root cause: no GET handler was ever registered for the literal path
/api/v1/agents/active — only POST /{agent_id}/activate existed (a documented
no-op, "global active agent has been removed"). Starlette resolves routing
by PATH first: GET /agents/active matched the PATH template of the
PUT/PATCH/DELETE /{agent_id} handlers (agent_id="active"), found no GET
registered there, and answered 405 instead of 404 or a real response. The
frontend's getActiveAgent() (frontend/src/api/client.ts) silently swallowed
the failure via .catch(() => ({active_agent_id: ''})), so nobody ever saw it
fail in the UI.

Fix: a GET /active handler mirroring the sibling POST's static "deprecated"
shape (there's no real global active-agent state to read — binding moved
per-conversation) so the route answers 200, matching the frontend's own
ActiveAgentResponse contract and its .catch() fallback shape exactly.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.tasks.control_plane.domain.ports import AgentUnavailable
from hermes.shell_server.cowork.agents_api import create_agents_router

pytestmark = pytest.mark.unit


class _FakeProxy:
    """Never actually called by GET /active (it's a static response) — exists
    so the app doesn't explode if some OTHER route on this router is hit."""

    async def call_list(self, member: str, *args):
        raise AgentUnavailable("no daemon in this test")

    async def call_mutator(self, member: str, *args) -> dict:
        raise AgentUnavailable("no daemon in this test")

    async def call_bool(self, member: str, *args) -> bool:
        raise AgentUnavailable("no daemon in this test")


def _client() -> TestClient:
    app = FastAPI()
    app.state.dbus_proxy = _FakeProxy()
    app.include_router(create_agents_router())
    return TestClient(app)


class TestGetActiveAgentRoute:
    def test_get_active_is_not_405(self) -> None:
        client = _client()
        r = client.get("/api/v1/agents/active")
        assert r.status_code != 405, (
            "GET /agents/active still 405s — Starlette is matching the "
            "PUT/PATCH/DELETE /{agent_id} path template before a real GET "
            "handler exists for the literal /active path."
        )

    def test_get_active_returns_200_with_the_frontend_contract_shape(self) -> None:
        client = _client()
        r = client.get("/api/v1/agents/active")

        assert r.status_code == 200
        body = r.json()
        # Matches frontend/src/api/types.ts ActiveAgentResponse exactly —
        # the same shape client.ts's own .catch() fallback already used.
        assert body["active_agent_id"] == ""
        assert body["deprecated"] is True

    def test_put_patch_delete_agent_id_active_are_unaffected(self) -> None:
        """Regression guard: adding the GET /active route must not steal the
        path from the pre-existing PUT/PATCH/DELETE /{agent_id} handlers —
        they should still resolve (and fail soft/hard on the fake proxy, not
        404/405) when "active" is used as a literal agent_id."""
        client = _client()

        put_r = client.put("/api/v1/agents/active", json={"name": "x"})
        assert put_r.status_code == 503  # AgentUnavailable -> _raise_503, not 404/405

        delete_r = client.delete("/api/v1/agents/active")
        assert delete_r.status_code == 503
