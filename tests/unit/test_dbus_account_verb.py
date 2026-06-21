"""GATE 0 / M7 🔒 — Regresión de EJECUCIÓN del verbo StageAccount del daemon.

Ejecuta stage_account contra un stage_dir/sentinel temporales (monkeypatch de
los defaults de shell_server.setup.api, que el handler reimporta por llamada).
Cubre: validación username/password (trust boundary), one-time (sentinel), authZ.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes.shell_server.setup.api as setup_api
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000
_UNAUTHORIZED_UID = 9999


class _NullApprovalGate:
    async def register_pending(self, *, proposal_id, **_) -> None: ...
    async def approve(self, *, proposal_id, approved_by) -> str:
        return ""
    async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
    async def verify_token(self, *, proposal_id, token) -> bool:
        return False
    async def approved_token_for(self, proposal_id) -> str | None:
        return None


@pytest.fixture
def wiring() -> DbusRuntimeServiceWiring:
    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
    )


@pytest.fixture
def staging(tmp_path: Path, monkeypatch):
    stage = tmp_path / "setup"
    sentinel = tmp_path / "account-applied"
    monkeypatch.setattr(setup_api, "_DEFAULT_STAGE_DIR", stage)
    monkeypatch.setattr(setup_api, "_DEFAULT_SENTINEL_FILE", sentinel)
    return stage, sentinel


def test_stage_account_writes_request(wiring, staging) -> None:
    stage, _ = staging
    r = wiring.stage_account(username="luiscorrea-dev", password="demo1234", sender_uid=_OPERATOR_UID)
    assert r["staged"] is True
    assert r["error"] is None
    assert (stage / "account-request.json").exists()


def test_stage_account_rejects_bad_username(wiring, staging) -> None:
    r = wiring.stage_account(username="Bad Name!", password="demo1234", sender_uid=_OPERATOR_UID)
    assert r["staged"] is False
    assert r["error"] == "invalid_username"


def test_stage_account_rejects_short_password(wiring, staging) -> None:
    r = wiring.stage_account(username="alex", password="short", sender_uid=_OPERATOR_UID)
    assert r["staged"] is False
    assert r["error"] == "invalid_password"


def test_stage_account_rejects_newline_password(wiring, staging) -> None:
    """chpasswd injection: una nueva línea en el password debe rechazarse."""
    r = wiring.stage_account(username="alex", password="abc\n12345", sender_uid=_OPERATOR_UID)
    assert r["staged"] is False
    assert r["error"] == "invalid_password"


def test_stage_account_one_time_via_sentinel(wiring, staging) -> None:
    _, sentinel = staging
    sentinel.write_text("applied")
    r = wiring.stage_account(username="alex", password="demo1234", sender_uid=_OPERATOR_UID)
    assert r["staged"] is False
    assert r["error"] == "already_configured"


def test_stage_account_denies_unauthorized(wiring, staging) -> None:
    with pytest.raises(DbusAuthorizationError):
        wiring.stage_account(username="alex", password="demo1234", sender_uid=_UNAUTHORIZED_UID)
