"""Integration test — PolicyApplier against the REAL D-Bus ServiceInterface.

F2 review fix: the previous unit-test coverage for access_scope stopped at
either the applier layer (FakeDbusProxy — never touches the adapter) or the
wiring layer (DbusRuntimeServiceWiring.set_agent_access_scope directly —
never touches the D-Bus method export). Neither caught "wired but not
exported": SetAgentAccessScope had no @method() on Runtime1ServiceInterface,
so the real client proxy resolved `call_set_agent_access_scope` to None and
every bundle with a scoped agent failed with AgentUnavailable.

This test exercises the REAL Runtime1ServiceInterface (the same class
dbus-fast exports on the system bus) and the REAL DbusRuntimeServiceWiring +
SqliteAgentAccessScopeRepo, wired together by an in-process proxy that calls
the interface's D-Bus methods directly (server-side sender-uid resolution is
faked the same way tests/security/test_dbus_sender_uid_race.py does — no
system bus is required to prove the production code path is reachable).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("dbus_fast")

from dbus_fast.service import ServiceInterface

from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry
from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (
    Runtime1ServiceInterface,
    _CURRENT_SENDER_VAR,
)
from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusRuntimeServiceWiring,
)
from hermes.capabilities.infrastructure.sqlite_agent_access_scope_repo import (
    SqliteAgentAccessScopeRepo,
)
from hermes.config_sync.applier import PolicyApplier
from hermes.config_sync.policy_document import PolicyPayload

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000
_DAEMON_TENANT_ID = "daemon-real-tenant"
_SENDER = ":1.100"


class _NullApprovalGate:
    async def register_pending(self, *, proposal_id, **_) -> None: ...
    async def approve(self, *, proposal_id, approved_by) -> str:
        return ""
    async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
    async def verify_token(self, *, proposal_id, token) -> bool:
        return False
    async def approved_token_for(self, proposal_id) -> str | None:
        return None


def _real_iface(tmp_path: Path) -> tuple[Runtime1ServiceInterface, SqliteAgentAccessScopeRepo]:
    """Build the REAL ServiceInterface + wiring + sqlite repos (no mocks)."""
    access_scope_repo = SqliteAgentAccessScopeRepo(db_path=tmp_path / "shell-state.db")
    agent_registry = SqliteAgentRegistry(db_path=tmp_path / "shell-state.db")
    wiring = DbusRuntimeServiceWiring(
        agent_state=None,
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
        agent_registry=agent_registry,
        access_scope_repo=access_scope_repo,
        # tenant_id is the DAEMON's own resolved tenant — never the applier's.
        tenant_id=_DAEMON_TENANT_ID,
    )
    iface = Runtime1ServiceInterface(wiring=wiring)  # type: ignore[arg-type]
    iface.attach_bus(object())  # any non-None value: only the None-check matters
    return iface, access_scope_repo


class _InProcessDbusProxy:
    """Routes PolicyApplier verb calls to the REAL ServiceInterface methods.

    Converts the snake_case verb (what the applier/allow-list use) to the
    exported PascalCase D-Bus member name — the exact inverse of what a real
    dbus-fast client proxy does, so this proves the SAME name the client would
    call resolves to a real, working method.

    dbus-fast's @method() decorator replaces the class attribute with a
    fire-and-forget wrapper (`fn(*args); return None`) meant for the bus's own
    dispatch loop — calling it directly would silently drop the coroutine.
    ServiceInterface._get_methods() exposes the ORIGINAL coroutine function
    (`_Method.fn`) for each declared method; invoking that directly is the
    supported no-bus-required technique already used by
    tests/security/test_dbus_sender_uid_race.py and
    tests/security/test_dbus_runtime1_contract.py in this repo.
    """

    def __init__(self, iface: Runtime1ServiceInterface) -> None:
        self._iface = iface
        self.calls: list[tuple[str, tuple]] = []
        self._methods_by_name = {
            m.name: m.fn for m in ServiceInterface._get_methods(iface)
        }

    @staticmethod
    def _member_name(verb: str) -> str:
        return "".join(part.capitalize() for part in verb.split("_"))

    async def _invoke(self, verb: str, *args: Any) -> Any:
        self.calls.append((verb, args))
        _CURRENT_SENDER_VAR.set(_SENDER)
        member = self._member_name(verb)
        fn = self._methods_by_name[member]
        return await fn(self._iface, *args)

    async def call_list(self, member: str, *args: Any) -> list[dict]:
        raw = await self._invoke(member, *args)
        return json.loads(raw) if raw else []

    async def call_dict(self, member: str, *args: Any) -> dict:
        raw = await self._invoke(member, *args)
        return json.loads(raw) if raw else {}

    async def call_mutator(self, member: str, *args: Any) -> dict:
        raw = await self._invoke(member, *args)
        if isinstance(raw, dict):
            return raw
        return json.loads(raw) if raw else {"ok": bool(raw)}

    async def call_bool(self, member: str, *args: Any) -> bool:
        return bool(await self._invoke(member, *args))


def _payload_with_scoped_agent() -> PolicyPayload:
    data = {
        "agents": [
            {
                "agent_id": "sales-agent",
                "name": "Sales",
                "access_scope": {
                    "enforced": True,
                    "native_tools": ["terminal"],
                    "policy_overlay": {"send_message": {"enabled": False}},
                },
            }
        ],
        "providers": [],
        "integrations": [],
        "mcp": [],
        "skills": [],
        "egress": {"allow_domains": []},
        "consents": [],
        "features": {"views": []},
        "license": {"plan": "starter", "max_agents": 5, "expires_at": "", "views": []},
    }
    return PolicyPayload.model_validate(data)


@patch(
    "hermes.agents_os.infrastructure.dbus_fast_runtime_adapter._get_connection_unix_user"
)
class TestApplyScopedAgentAgainstRealServiceInterface:
    async def test_apply_lands_scope_with_ok_true(
        self, mock_uid, tmp_path: Path
    ) -> None:
        mock_uid.return_value = _OPERATOR_UID
        iface, repo = _real_iface(tmp_path)
        proxy = _InProcessDbusProxy(iface)
        payload = _payload_with_scoped_agent()

        result = await PolicyApplier(proxy).apply(
            payload, current_agents=[], tenant_id="cloud-bundle-tenant-should-be-ignored"
        )

        assert result.ok is True
        assert not result.failed

    async def test_row_lands_managed_by_cloud_with_daemon_tenant(
        self, mock_uid, tmp_path: Path
    ) -> None:
        mock_uid.return_value = _OPERATOR_UID
        iface, repo = _real_iface(tmp_path)
        proxy = _InProcessDbusProxy(iface)
        payload = _payload_with_scoped_agent()

        result = await PolicyApplier(proxy).apply(
            payload, current_agents=[], tenant_id="cloud-bundle-tenant-should-be-ignored"
        )
        assert result.ok is True

        scope = repo.get_scope("sales-agent", _DAEMON_TENANT_ID)
        assert scope is not None, (
            "scope must land under the DAEMON-derived tenant, not the applier's "
            "bundle tenant_id — this is what security_hook.get_scope will query"
        )
        assert scope.managed_by == "cloud"
        assert scope.enforced is True
        assert scope.native_tools == frozenset({"terminal"})
        assert scope.policy_overlay == {"send_message": {"enabled": False}}

        # The (spoofed) bundle tenant must NOT have received the row.
        assert repo.get_scope("sales-agent", "cloud-bundle-tenant-should-be-ignored") is None

    async def test_second_identical_apply_is_idempotent(
        self, mock_uid, tmp_path: Path
    ) -> None:
        mock_uid.return_value = _OPERATOR_UID
        iface, repo = _real_iface(tmp_path)
        proxy = _InProcessDbusProxy(iface)
        payload = _payload_with_scoped_agent()

        first = await PolicyApplier(proxy).apply(payload, current_agents=[], tenant_id="t")
        assert first.ok is True

        # Simulate the sync loop's next tick: same bundle, agent now cloud-managed.
        current_agents = await proxy.call_list("list_agents")
        second = await PolicyApplier(proxy).apply(
            payload, current_agents=current_agents, tenant_id="t"
        )

        assert second.ok is True
        assert not second.failed

        rows = repo.list_by_agent("sales-agent")
        assert len(rows) == 1, "upsert must replace the (tenant_id, agent_id) row, not duplicate it"
        assert rows[0].managed_by == "cloud"
