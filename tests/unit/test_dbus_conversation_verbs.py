"""GATE 0 / M2 🔒 — Regresión de EJECUCIÓN de los verbos de chat del daemon.

Como en los providers (M1), los tests de contrato sólo verifican firmas D-Bus.
Esta suite EJECUTA list/get/delete contra un SQLiteConversationRepository real para
cazar NameError / firma rota / authZ floja antes de un bake.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.tasks.infrastructure.sqlite_conversation_repo import SQLiteConversationRepository
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


def _make(tmp_path: Path) -> tuple[DbusRuntimeServiceWiring, SQLiteConversationRepository]:
    repo = SQLiteConversationRepository(db_path=tmp_path / "chat.db")
    wiring = DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
        conversation_repo=repo,
    )
    return wiring, repo


def _seed(repo: SQLiteConversationRepository, *, agent_id: str | None = None):
    conv_id = uuid4()
    repo.create_or_touch(
        conversation_id=conv_id, first_user_message="Hola agente", agent_id=agent_id
    )
    repo.append_message(conversation_id=conv_id, role="user", content="Hola agente")
    repo.append_message(conversation_id=conv_id, role="assistant", content="¡Hola! ¿En qué ayudo?")
    return conv_id


def test_list_conversations_executes(tmp_path: Path) -> None:
    wiring, repo = _make(tmp_path)
    assert wiring.list_conversations() == []
    conv_id = _seed(repo)
    listed = wiring.list_conversations()
    assert len(listed) == 1
    assert listed[0]["conversation_id"] == str(conv_id)
    assert listed[0]["message_count"] == 2
    assert listed[0]["title"] == "Hola agente"


def test_list_filters_by_agent(tmp_path: Path) -> None:
    wiring, repo = _make(tmp_path)
    _seed(repo, agent_id="agent-a")
    _seed(repo, agent_id="agent-b")
    assert len(wiring.list_conversations()) == 2  # '' → todas
    only_a = wiring.list_conversations(agent_id="agent-a")
    assert len(only_a) == 1
    assert only_a[0]["agent_id"] == "agent-a"


def test_get_conversation_returns_messages(tmp_path: Path) -> None:
    wiring, repo = _make(tmp_path)
    conv_id = _seed(repo)
    d = wiring.get_conversation(conversation_id=str(conv_id))
    assert d["conversation_id"] == str(conv_id)
    roles = [m["role"] for m in d["messages"]]
    assert roles == ["user", "assistant"]
    assert d["messages"][1]["content"] == "¡Hola! ¿En qué ayudo?"


def test_get_missing_returns_empty(tmp_path: Path) -> None:
    wiring, _ = _make(tmp_path)
    assert wiring.get_conversation(conversation_id=str(uuid4())) == {}


def test_delete_conversation_requires_auth(tmp_path: Path) -> None:
    wiring, repo = _make(tmp_path)
    conv_id = _seed(repo)
    with pytest.raises(DbusAuthorizationError):
        wiring.delete_conversation(conversation_id=str(conv_id), sender_uid=_UNAUTHORIZED_UID)
    # sigue existiendo tras el intento no autorizado
    assert len(wiring.list_conversations()) == 1


def test_delete_conversation_executes(tmp_path: Path) -> None:
    wiring, repo = _make(tmp_path)
    conv_id = _seed(repo)
    assert wiring.delete_conversation(
        conversation_id=str(conv_id), sender_uid=_OPERATOR_UID
    ) is True
    assert wiring.list_conversations() == []


def test_reads_without_repo_are_safe(tmp_path: Path) -> None:
    """Sin conversation_repo inyectado: lecturas devuelven vacío, no crashean."""
    wiring = DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
        conversation_repo=None,
    )
    assert wiring.list_conversations() == []
    assert wiring.get_conversation(conversation_id=str(uuid4())) == {}
