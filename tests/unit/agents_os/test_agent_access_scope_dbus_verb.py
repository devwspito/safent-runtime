"""set_agent_access_scope D-Bus verb — Enterprise Fase 2 Phase 3.

Covers DbusRuntimeServiceWiring.set_agent_access_scope:
  - Unauthorized sender_uid raises DbusAuthorizationError (never executes).
  - updated_by is ALWAYS sender_uid, NEVER from the payload (CWE-862) — proven
    both by the landed row AND by scope_json unable to smuggle an updated_by
    key at all (unknown keys are rejected at the trust boundary).
  - Unknown keys in scope_json are rejected; nothing is persisted.
  - A valid scope lands in the repo with managed_by="cloud".
  - Malformed JSON / wrong types / oversized lists are rejected (CWE-20).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.capabilities.infrastructure.sqlite_agent_access_scope_repo import (
    SqliteAgentAccessScopeRepo,
)
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000
_UNAUTHORIZED_UID = 9999
_TENANT_ID = "tenant-x"


class _NullApprovalGate:
    async def register_pending(self, *, proposal_id, **_) -> None: ...
    async def approve(self, *, proposal_id, approved_by) -> str:
        return ""
    async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
    async def verify_token(self, *, proposal_id, token) -> bool:
        return False
    async def approved_token_for(self, proposal_id) -> str | None:
        return None


def _make_wiring(tmp_path: Path) -> tuple[DbusRuntimeServiceWiring, SqliteAgentAccessScopeRepo]:
    repo = SqliteAgentAccessScopeRepo(db_path=tmp_path / "shell-state.db")
    wiring = DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
        access_scope_repo=repo,
    )
    return wiring, repo


class TestUnauthorized:
    @pytest.mark.asyncio
    async def test_unauthorized_uid_raises(self, tmp_path: Path) -> None:
        wiring, _repo = _make_wiring(tmp_path)
        with pytest.raises(DbusAuthorizationError):
            await wiring.set_agent_access_scope(
                agent_id="agent-a",
                scope_json="{}",
                tenant_id=_TENANT_ID,
                sender_uid=_UNAUTHORIZED_UID,
            )

    @pytest.mark.asyncio
    async def test_unauthorized_call_never_persists(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        with pytest.raises(DbusAuthorizationError):
            await wiring.set_agent_access_scope(
                agent_id="agent-a",
                scope_json='{"enforced": true}',
                tenant_id=_TENANT_ID,
                sender_uid=_UNAUTHORIZED_UID,
            )
        assert repo.get_scope("agent-a", _TENANT_ID) is None


class TestUpdatedByFromSenderNeverFromPayload:
    @pytest.mark.asyncio
    async def test_updated_by_equals_sender_uid(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json='{"enforced": true}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        scope = repo.get_scope("agent-a", _TENANT_ID)
        assert scope is not None
        assert scope.updated_by == _OPERATOR_UID

    @pytest.mark.asyncio
    async def test_updated_by_key_in_payload_is_rejected_as_unknown(self, tmp_path: Path) -> None:
        """The wire shape has no 'updated_by' key at all — an attempt to smuggle
        one in scope_json is rejected outright (unknown key), proving the value
        can never come from the payload."""
        wiring, repo = _make_wiring(tmp_path)
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json='{"enforced": true, "updated_by": 42}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is False
        assert repo.get_scope("agent-a", _TENANT_ID) is None


class TestUnknownKeysRejected:
    @pytest.mark.asyncio
    async def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json='{"enforced": true, "sneaky_extra_field": "x"}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is False
        assert "error" in result
        assert repo.get_scope("agent-a", _TENANT_ID) is None


class TestScopeLandsManagedByCloud:
    @pytest.mark.asyncio
    async def test_full_scope_lands_correctly(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        scope_json = (
            '{"enforced": true, "cerebro_unrestricted": false, '
            '"native_tools": ["terminal", "read_file"], '
            '"policy_overlay": {"send_message": {"enabled": false}}, '
            '"views": ["calendar"]}'
        )
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json=scope_json,
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is True
        assert "scope_id" in result

        scope = repo.get_scope("agent-a", _TENANT_ID)
        assert scope is not None
        assert scope.enforced is True
        assert scope.cerebro_unrestricted is False
        assert scope.native_tools == frozenset({"terminal", "read_file"})
        assert scope.policy_overlay == {"send_message": {"enabled": False}}
        assert scope.views == ("calendar",)
        assert scope.managed_by == "cloud"
        assert scope.tenant_id == _TENANT_ID
        assert scope.agent_id == "agent-a"

    @pytest.mark.asyncio
    async def test_defaults_when_only_partial_fields_present(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        await wiring.set_agent_access_scope(
            agent_id="agent-b",
            scope_json="{}",
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        scope = repo.get_scope("agent-b", _TENANT_ID)
        assert scope is not None
        assert scope.enforced is False
        assert scope.cerebro_unrestricted is True
        assert scope.native_tools == frozenset()
        assert scope.managed_by == "cloud"

    @pytest.mark.asyncio
    async def test_upsert_replaces_previous_scope_for_same_agent(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json='{"enforced": false}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json='{"enforced": true, "native_tools": ["terminal"]}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        scope = repo.get_scope("agent-a", _TENANT_ID)
        assert scope is not None
        assert scope.enforced is True
        assert scope.native_tools == frozenset({"terminal"})


class TestMalformedInputRejectedCwe20:
    @pytest.mark.asyncio
    async def test_invalid_json_rejected(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json="not-json{{{",
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is False
        assert repo.get_scope("agent-a", _TENANT_ID) is None

    @pytest.mark.asyncio
    async def test_non_object_json_rejected(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json="[1, 2, 3]",
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_native_tools_not_a_list_rejected(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json='{"native_tools": "terminal"}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_native_tools_over_256_rejected(self, tmp_path: Path) -> None:
        import json

        wiring, repo = _make_wiring(tmp_path)
        scope_json = json.dumps({"native_tools": [f"tool{i}" for i in range(257)]})
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json=scope_json,
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is False
        assert repo.get_scope("agent-a", _TENANT_ID) is None

    @pytest.mark.asyncio
    async def test_enforced_wrong_type_rejected(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json='{"enforced": "yes"}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is False


class TestPolicyOverlayInnerShapeRejectedF3:
    """F3 review fix: policy_overlay inner shape must be dict[str, dict[str,
    bool]] (mirrors the cloud AccessScopeSpec.policy_overlay contract) — a
    wrong-typed "enabled" value must be rejected AT THIS trust boundary, not
    only fail-closed downstream in AgentToolPolicyView."""

    @pytest.mark.asyncio
    async def test_enabled_wrong_type_rejected(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json='{"policy_overlay": {"terminal": {"enabled": "yes"}}}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is False
        assert repo.get_scope("agent-a", _TENANT_ID) is None

    @pytest.mark.asyncio
    async def test_non_dict_entry_rejected(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json='{"policy_overlay": {"terminal": true}}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is False
        assert repo.get_scope("agent-a", _TENANT_ID) is None

    @pytest.mark.asyncio
    async def test_well_formed_overlay_still_accepted(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json='{"policy_overlay": {"terminal": {"enabled": false}}}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is True
        scope = repo.get_scope("agent-a", _TENANT_ID)
        assert scope is not None
        assert scope.policy_overlay == {"terminal": {"enabled": False}}


class TestNativeToolsViewsStringLengthCapF3:
    """F3 review fix: each native_tools/views entry is capped at 128 chars —
    the list-length cap alone doesn't bound a single oversized string."""

    @pytest.mark.asyncio
    async def test_over_long_native_tool_name_rejected(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json=f'{{"native_tools": ["{"a" * 129}"]}}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is False
        assert repo.get_scope("agent-a", _TENANT_ID) is None

    @pytest.mark.asyncio
    async def test_over_long_view_name_rejected(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json=f'{{"views": ["{"b" * 129}"]}}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is False
        assert repo.get_scope("agent-a", _TENANT_ID) is None

    @pytest.mark.asyncio
    async def test_exactly_128_chars_accepted(self, tmp_path: Path) -> None:
        wiring, repo = _make_wiring(tmp_path)
        result = await wiring.set_agent_access_scope(
            agent_id="agent-a",
            scope_json=f'{{"native_tools": ["{"a" * 128}"]}}',
            tenant_id=_TENANT_ID,
            sender_uid=_OPERATOR_UID,
        )
        assert result["ok"] is True
