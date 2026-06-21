"""Gobernanza de agentes por D-Bus (wiring): lecturas libres, mutadores con
autoría por sender_uid (fail-closed). Estado nativo del daemon (Principio 0)."""

from __future__ import annotations

import asyncio

import pytest

from hermes.agents.application.serialization import draft_from_dict
from hermes.agents.domain.agent import DEFAULT_AGENT_ID
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry

_OPERATOR_UID = 1000


def _wiring(tmp_path):
    reg = SqliteAgentRegistry(db_path=tmp_path / "shell-state.db")
    wiring = DbusRuntimeServiceWiring(
        agent_state=None,
        approval_gate=None,
        authorized_uids=frozenset({_OPERATOR_UID}),
        agent_registry=reg,
    )
    return wiring, reg


def test_list_and_active_are_readonly(tmp_path):
    wiring, _ = _wiring(tmp_path)
    agents = wiring.list_agents()
    assert len(agents) == 1
    assert agents[0]["agent_id"] == DEFAULT_AGENT_ID
    assert agents[0]["is_default"] is True
    assert wiring.get_active_agent() == DEFAULT_AGENT_ID


def test_create_requires_authorized_uid(tmp_path):
    wiring, reg = _wiring(tmp_path)
    draft = draft_from_dict({"name": "X"})
    with pytest.raises(DbusAuthorizationError):
        asyncio.run(wiring.create_agent(draft=draft, sender_uid=999))
    assert len(reg.list_agents()) == 1  # fail-closed: no se creó


def test_create_set_active_delete_roundtrip(tmp_path):
    wiring, reg = _wiring(tmp_path)
    draft = draft_from_dict({"name": "Ventas", "instructions": "tono comercial"})
    created = asyncio.run(wiring.create_agent(draft=draft, sender_uid=_OPERATOR_UID))
    assert created["name"] == "Ventas"
    assert len(reg.list_agents()) == 2

    asyncio.run(
        wiring.set_active_agent(agent_id=created["agent_id"], sender_uid=_OPERATOR_UID)
    )
    assert wiring.get_active_agent() == created["agent_id"]

    updated = asyncio.run(
        wiring.update_agent(
            agent_id=created["agent_id"],
            draft=draft_from_dict({"name": "Ventas Pro"}),
            sender_uid=_OPERATOR_UID,
        )
    )
    assert updated["name"] == "Ventas Pro"

    asyncio.run(
        wiring.delete_agent(agent_id=created["agent_id"], sender_uid=_OPERATOR_UID)
    )
    # borrar el activo reactiva el default
    assert wiring.get_active_agent() == DEFAULT_AGENT_ID
    assert len(reg.list_agents()) == 1


def test_mutators_unauthorized_fail_closed(tmp_path):
    wiring, reg = _wiring(tmp_path)
    created = asyncio.run(
        wiring.create_agent(
            draft=draft_from_dict({"name": "Tmp"}), sender_uid=_OPERATOR_UID
        )
    )
    aid = created["agent_id"]
    with pytest.raises(DbusAuthorizationError):
        asyncio.run(wiring.set_active_agent(agent_id=aid, sender_uid=7))
    with pytest.raises(DbusAuthorizationError):
        asyncio.run(wiring.delete_agent(agent_id=aid, sender_uid=7))
    assert {a["agent_id"] for a in wiring.list_agents()} == {DEFAULT_AGENT_ID, aid}
