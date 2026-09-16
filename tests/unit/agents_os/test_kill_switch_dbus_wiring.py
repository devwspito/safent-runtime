"""025 Top-KILL — D-Bus wiring for the kill-switch REST surface.

Covers:
  - get_kill_switch_status() reflects AgentStatePort.status() (the verb
    GetKillSwitchStatus JSON-serializes for GET /api/v1/security/kill-switch).
  - request_pause() (Pause D-Bus verb) also requests COOPERATIVE cancellation
    of every task_id with live activity (best-effort "running turns are
    cancelled") — via the SAME registry CancelTask already uses, no new
    cancellation primitive.
  - Best-effort: request_pause() never raises even with no live activity.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import DbusRuntimeServiceWiring
from hermes.runtime import live_activity
from hermes.shell_server.providers.repo import SQLiteProviderRepository
from hermes.shell_server.security.secrets import SecretsVault
from hermes.tasks.domain.task_cancel_registry import get_cancel_registry
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000


class _NullApprovalGate:
    async def register_pending(self, *, proposal_id, **_) -> None: ...
    async def approve(self, *, proposal_id, approved_by) -> str:
        return ""
    async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
    async def verify_token(self, *, proposal_id, token) -> bool:
        return False
    async def approved_token_for(self, proposal_id) -> str | None:
        return None


def _make_wiring(tmp_path: Path, *, agent_state=None) -> DbusRuntimeServiceWiring:
    vault = SecretsVault(master_key=os.urandom(32))
    repo = SQLiteProviderRepository(db_path=tmp_path / "providers.db", vault=vault)
    return DbusRuntimeServiceWiring(
        agent_state=agent_state or InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
        provider_repo=repo,
    )


class TestGetKillSwitchStatus:
    @pytest.mark.asyncio
    async def test_status_reflects_fresh_state(self, tmp_path: Path) -> None:
        wiring = _make_wiring(tmp_path)
        status = await wiring.get_kill_switch_status()
        assert status["engaged"] is False

    @pytest.mark.asyncio
    async def test_status_reflects_engaged_state_after_request_pause(self, tmp_path: Path) -> None:
        wiring = _make_wiring(tmp_path)
        await wiring.request_pause(reason="prueba", sender_uid=_OPERATOR_UID)

        status = await wiring.get_kill_switch_status()

        assert status["engaged"] is True
        assert status["reason"] == "prueba"


class TestRequestPauseProvenance:
    """025 re-verificación d2eb8c6 (menor nuevo): AGENT_PAUSED had no closed
    provenance vocabulary — only `actor` (uid) and whatever free text the
    caller wrote distinguished two pauses of different origin. request_pause
    now derives AgentPauseProvenance from the ALREADY-authorized sender_uid:
    proxy_uid (the REST API via the shell-server) => api; any direct
    authorized uid (host/TUI) => host_cli."""

    @pytest.mark.asyncio
    async def test_direct_authorized_uid_gets_host_cli_provenance(
        self, tmp_path: Path
    ) -> None:
        state = InMemoryAgentState()
        wiring = _make_wiring(tmp_path, agent_state=state)

        await wiring.request_pause(reason="freno", sender_uid=_OPERATOR_UID)

        assert state.pause_calls[-1]["provenance"] == "host_cli"

    @pytest.mark.asyncio
    async def test_proxy_uid_with_valid_token_gets_api_provenance(
        self, tmp_path: Path
    ) -> None:
        from hermes.shell_server.security.operator_token import (
            OperatorTokenMinter,
            OperatorTokenVerifier,
        )

        vault = SecretsVault(master_key=os.urandom(32))
        repo = SQLiteProviderRepository(db_path=tmp_path / "providers.db", vault=vault)
        signing_key = os.urandom(32)
        minter = OperatorTokenMinter(signing_key=signing_key, expiry_s=30)
        verifier = OperatorTokenVerifier(signing_key=signing_key)
        proxy_uid = 880
        state = InMemoryAgentState()
        wiring = DbusRuntimeServiceWiring(
            agent_state=state,
            approval_gate=_NullApprovalGate(),
            authorized_uids=frozenset({_OPERATOR_UID}),
            provider_repo=repo,
            proxy_uid=proxy_uid,
            operator_token_verifier=verifier,
        )
        token = minter.mint(operator_id=str(UUID(int=_OPERATOR_UID)), operation="request_pause")

        await wiring.request_pause(
            reason="freno via api", sender_uid=proxy_uid, operator_token=token
        )

        assert state.pause_calls[-1]["provenance"] == "api"


class TestRequestPauseCancelsLiveTurns:
    @pytest.mark.asyncio
    async def test_pause_requests_cancellation_of_live_task(self, tmp_path: Path) -> None:
        wiring = _make_wiring(tmp_path)
        task_id = "6b3b6b9e-3b7e-4e3b-9b3b-6b3b6b9e3b7e"
        registry = get_cancel_registry()
        live_activity.record(task_id, "agent-a", "terminal")
        try:
            await wiring.request_pause(reason="freno", sender_uid=_OPERATOR_UID)

            assert registry.is_cancelled(UUID(task_id)) is True
        finally:
            live_activity.clear(task_id)
            registry.clear(UUID(task_id))

    @pytest.mark.asyncio
    async def test_pause_never_raises_with_no_live_activity(self, tmp_path: Path) -> None:
        wiring = _make_wiring(tmp_path)

        await wiring.request_pause(reason="freno sin actividad", sender_uid=_OPERATOR_UID)

        status = await wiring.get_kill_switch_status()
        assert status["engaged"] is True
