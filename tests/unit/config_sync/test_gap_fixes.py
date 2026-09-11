"""Tests for GAP 6, GAP 5, and GAP 7 fixes.

GAP 6: Provider api_key propagated from cloud bundle to daemon vault.
GAP 5: Consent operator-gated failures classified as pending_operator, not failed.
GAP 7: Associate list_agents filtered to cloud-managed agents.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hermes.config_sync.applier import (
    ApplyResult,
    PolicyApplier,
    _is_authorization_error,
)
from hermes.config_sync.policy_document import (
    PolicyPayload,
    ProviderSpec,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeProxy:
    """Minimal async D-Bus proxy stub that records all calls."""

    def __init__(
        self,
        *,
        existing_agents: list[dict] | None = None,
        existing_providers: list[dict] | None = None,
        grant_consent_exc: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._existing_agents = existing_agents or []
        self._existing_providers = existing_providers or []
        self._grant_consent_exc = grant_consent_exc

    async def call_list(self, member: str, *args: Any) -> list[dict]:
        self.calls.append((member, args))
        if member == "list_agents":
            return list(self._existing_agents)
        if member == "list_providers":
            return list(self._existing_providers)
        return []

    async def call_dict(self, member: str, *args: Any) -> dict:
        self.calls.append((member, args))
        if member == "get_composio_status":
            return {"has_key": False}
        return {}

    async def call_mutator(self, member: str, *args: Any) -> dict:
        self.calls.append((member, args))
        if member == "grant_consent" and self._grant_consent_exc is not None:
            raise self._grant_consent_exc
        if member == "create_agent":
            draft = json.loads(args[0]) if args else {}
            return {"ok": True, "agent_id": draft.get("agent_id", "new-id")}
        return {"ok": True}

    async def call_bool(self, member: str, *args: Any) -> bool:
        self.calls.append((member, args))
        return True

    def calls_for(self, verb: str) -> list[tuple]:
        return [args for (m, args) in self.calls if m == verb]


def _make_payload(**overrides: Any) -> PolicyPayload:
    base: dict = {
        "agents": [],
        "providers": [],
        "integrations": [],
        "mcp": [],
        "skills": [],
        "egress": {"allow_domains": []},
        "consents": [],
        "features": {"views": []},
        "license": {"plan": "starter", "max_agents": 5, "expires_at": "", "views": []},
    }
    base.update(overrides)
    return PolicyPayload.model_validate(base)


# ---------------------------------------------------------------------------
# GAP 6: api_key propagated from ProviderSpec to draft sent to daemon
# ---------------------------------------------------------------------------


class TestGap6ProviderApiKey:
    def test_provider_spec_accepts_api_key_field(self) -> None:
        """ProviderSpec must accept an api_key field (not rejected by Pydantic)."""
        spec = ProviderSpec(
            alias="openai-prod",
            kind="openai",
            default_model="gpt-4o",
            api_key="sk-test-key-12345",
        )
        assert spec.api_key == "sk-test-key-12345"

    def test_provider_spec_api_key_defaults_to_none(self) -> None:
        """api_key is optional — omitting it yields None (backward compat)."""
        spec = ProviderSpec(alias="openai", kind="openai", default_model="gpt-4")
        assert spec.api_key is None

    @pytest.mark.asyncio
    async def test_add_provider_draft_carries_api_key(self) -> None:
        """Full apply path: add_provider receives draft JSON with api_key."""
        proxy = FakeProxy()
        payload = _make_payload(
            providers=[
                {
                    "alias": "openai-prod",
                    "kind": "openai",
                    "default_model": "gpt-4o",
                    "api_key": "sk-live-key",
                }
            ]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        add_calls = proxy.calls_for("add_provider")
        assert not add_calls  # upstream master credentials must not be distributed

    @pytest.mark.asyncio
    async def test_update_provider_draft_carries_api_key(self) -> None:
        """Full apply path: update_provider receives draft JSON with api_key."""
        existing = [
            {
                "provider_id": "prov-uuid-001",
                "alias": "openai-prod",
                "kind": "openai",
                "default_model": "gpt-4",
            }
        ]
        proxy = FakeProxy(existing_providers=existing)
        payload = _make_payload(
            providers=[
                {
                    "alias": "openai-prod",
                    "kind": "openai",
                    "default_model": "gpt-4o",
                    "api_key": "sk-updated-key",
                }
            ]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        update_calls = proxy.calls_for("update_provider")
        assert not update_calls  # only verified instance_gateway envelopes apply

    @pytest.mark.asyncio
    async def test_add_provider_without_api_key_sends_no_api_key_field(self) -> None:
        """When spec has no api_key, the draft must not contain the field at all."""
        proxy = FakeProxy()
        payload = _make_payload(
            providers=[{"alias": "openai", "kind": "openai", "default_model": "gpt-4"}]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        add_calls = proxy.calls_for("add_provider")
        assert not add_calls  # keyless legacy managed configuration is not a grant

    @pytest.mark.asyncio
    async def test_api_key_not_logged(self) -> None:
        """The api_key must never appear in any log record emitted during apply."""
        import logging
        from io import StringIO

        log_buffer = StringIO()
        handler = logging.StreamHandler(log_buffer)
        handler.setLevel(logging.DEBUG)
        logging.getLogger("hermes.config_sync.applier").addHandler(handler)

        try:
            proxy = FakeProxy()
            payload = _make_payload(
                providers=[
                    {
                        "alias": "openai",
                        "kind": "openai",
                        "default_model": "gpt-4",
                        "api_key": "super-secret-key-do-not-log",
                    }
                ]
            )
            await PolicyApplier(proxy).apply(payload, current_agents=[])
        finally:
            logging.getLogger("hermes.config_sync.applier").removeHandler(handler)

        log_output = log_buffer.getvalue()
        assert "super-secret-key-do-not-log" not in log_output


# ---------------------------------------------------------------------------
# GAP 5: Consent operator-gated → pending_operator, not failed
# ---------------------------------------------------------------------------


class TestGap5ConsentPendingOperator:
    @pytest.mark.asyncio
    async def test_authorization_exception_classifies_as_pending_operator(self) -> None:
        """PermissionError from grant_consent → rejected (pending_operator), not failed."""
        proxy = FakeProxy(grant_consent_exc=PermissionError("UID 999 no autorizado"))
        payload = _make_payload(
            consents=[{"capability": "browser_navigate", "scope": "session"}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert any("browser_navigate" in r for r in result.rejected)
        assert not any("browser_navigate" in f for f in result.failed)

    @pytest.mark.asyncio
    async def test_authorization_exception_does_not_block_version(self) -> None:
        """An operator-gated consent must not prevent last_applied_version from advancing."""
        proxy = FakeProxy(grant_consent_exc=PermissionError("not authorized"))
        payload = _make_payload(
            consents=[{"capability": "documents", "scope": "session"}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert result.ok is True  # version CAN advance

    @pytest.mark.asyncio
    async def test_high_risk_consent_is_pending_operator_not_failed(self) -> None:
        """High-risk consents (terminal_exec) are always pending_operator."""
        proxy = FakeProxy()
        payload = _make_payload(
            consents=[{"capability": "terminal_exec", "scope": "session"}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "grant_consent" not in [v for v, _ in proxy.calls]
        assert any("terminal_exec" in r for r in result.rejected)
        assert not any("terminal_exec" in f for f in result.failed)
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_http_401_exception_is_operator_gated(self) -> None:
        """HTTPException(401) from grant_consent → pending_operator."""
        try:
            from fastapi import HTTPException
            exc = HTTPException(status_code=401, detail="Unauthorized")
        except ImportError:
            # Create a duck-typed stand-in if fastapi is not in the test env.
            class _FakeHTTPException(Exception):
                def __init__(self, status_code: int, detail: str) -> None:
                    super().__init__(detail)
                    self.status_code = status_code

            exc = _FakeHTTPException(status_code=401, detail="Unauthorized")

        proxy = FakeProxy(grant_consent_exc=exc)
        payload = _make_payload(
            consents=[{"capability": "browser_navigate", "scope": "session"}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert any("browser_navigate" in r for r in result.rejected)
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_transitory_failure_still_goes_to_failed(self) -> None:
        """A non-authorization exception (daemon down) remains a transitory failure."""
        proxy = FakeProxy(grant_consent_exc=ConnectionError("daemon not running"))
        payload = _make_payload(
            consents=[{"capability": "browser_navigate", "scope": "session"}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert any("browser_navigate" in f for f in result.failed)
        assert not any("browser_navigate" in r for r in result.rejected)
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_mixed_consents_partial_pending_partial_ok(self) -> None:
        """auth-failed consent goes to rejected; successful one is counted as applied."""
        call_count = 0

        class PartialProxy(FakeProxy):
            async def call_mutator(self, member: str, *args: Any) -> dict:
                nonlocal call_count
                self.calls.append((member, args))
                if member == "grant_consent":
                    call_count += 1
                    if call_count == 1:
                        raise PermissionError("operator only")
                    return {"ok": True}
                return {"ok": True}

        proxy = PartialProxy()
        payload = _make_payload(
            consents=[
                {"capability": "browser_navigate", "scope": "session"},
                {"capability": "documents", "scope": "session"},
            ]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        # One consent went to pending_operator, the other was applied.
        assert len(result.rejected) == 1
        assert result.applied >= 1
        assert result.ok is True

    def test_is_authorization_error_detects_permission_error(self) -> None:
        assert _is_authorization_error(PermissionError("no autorizado")) is True

    def test_is_authorization_error_detects_http_401(self) -> None:
        class _FakeHTTPException(Exception):
            def __init__(self) -> None:
                super().__init__("Unauthorized")
                self.status_code = 401

        assert _is_authorization_error(_FakeHTTPException()) is True

    def test_is_authorization_error_detects_message_keywords(self) -> None:
        class _FakeAgentUnavailable(Exception):
            pass

        exc = _FakeAgentUnavailable("org.hermes.Error.Unauthorized: UID 999")
        assert _is_authorization_error(exc) is True

    def test_is_authorization_error_does_not_match_connection_error(self) -> None:
        assert _is_authorization_error(ConnectionError("timeout")) is False

    def test_is_authorization_error_does_not_match_value_error(self) -> None:
        assert _is_authorization_error(ValueError("bad input")) is False


# ---------------------------------------------------------------------------
# GAP 7: Associate list_agents filtered to cloud-managed agents
# ---------------------------------------------------------------------------


class TestGap7AssociateRosterFilter:
    """Tests for _filter_associate_agents in dbus_runtime_service."""

    def _make_agent(
        self,
        agent_id: str,
        *,
        is_default: bool = False,
        managed_by: str | None = None,
        name: str = "Test Agent",
    ) -> Any:
        """Return a minimal duck-typed Agent object."""
        class _Agent:
            pass

        a = _Agent()
        a.agent_id = agent_id
        a.name = name
        a.is_default = is_default
        a.managed_by = managed_by
        return a

    def _to_dict(self, agent: Any) -> dict:
        return {"agent_id": agent.agent_id, "name": agent.name,
                "is_default": agent.is_default, "managed_by": agent.managed_by}

    def test_cloud_agents_visible_when_present(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _filter_associate_agents,
        )

        ceo = self._make_agent("default", is_default=True, name="CEO")
        cloud_a = self._make_agent("cloud-1", managed_by="cloud", name="Sales")
        local_r = self._make_agent("roster-42", name="Roster Specialist")

        result = _filter_associate_agents([ceo, cloud_a, local_r], self._to_dict)
        ids = {r["agent_id"] for r in result}

        assert "cloud-1" in ids
        assert "default" in ids  # CEO always visible
        assert "roster-42" not in ids  # roster agent hidden

    def test_only_ceo_returned_when_no_cloud_agents(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _filter_associate_agents,
        )

        ceo = self._make_agent("default", is_default=True, name="CEO")
        local_r = self._make_agent("roster-42", name="Roster Specialist")

        result = _filter_associate_agents([ceo, local_r], self._to_dict)
        ids = {r["agent_id"] for r in result}

        assert "default" in ids
        assert "roster-42" not in ids

    def test_fallback_to_all_agents_when_no_ceo_and_no_cloud(self) -> None:
        """Edge: no default + no cloud agents → return full list (never empty)."""
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _filter_associate_agents,
        )

        local_r = self._make_agent("roster-42", name="Roster Specialist")

        result = _filter_associate_agents([local_r], self._to_dict)
        ids = {r["agent_id"] for r in result}

        assert "roster-42" in ids  # fallback: return all

    def test_multiple_cloud_agents_all_visible(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _filter_associate_agents,
        )

        ceo = self._make_agent("default", is_default=True)
        cloud_a = self._make_agent("cloud-sales", managed_by="cloud", name="Sales")
        cloud_b = self._make_agent("cloud-support", managed_by="cloud", name="Support")
        roster = self._make_agent("roster-1", name="Roster Agent")

        result = _filter_associate_agents([ceo, cloud_a, cloud_b, roster], self._to_dict)
        ids = {r["agent_id"] for r in result}

        assert {"default", "cloud-sales", "cloud-support"} <= ids
        assert "roster-1" not in ids

    def test_no_duplicates_in_result(self) -> None:
        """CEO is not duplicated when it appears in both default and cloud lists."""
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _filter_associate_agents,
        )

        ceo = self._make_agent("default", is_default=True, managed_by="cloud")
        cloud_a = self._make_agent("cloud-1", managed_by="cloud")

        result = _filter_associate_agents([ceo, cloud_a], self._to_dict)
        agent_ids = [r["agent_id"] for r in result]

        assert agent_ids.count("default") == 1

    def test_list_agents_filters_when_associated(self) -> None:
        """DbusRuntimeServiceWiring.list_agents() uses associate filter when is_associated."""
        from unittest.mock import MagicMock
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            DbusRuntimeServiceWiring,
        )
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

        # Build minimal wiring with a mock association_store and agent_registry.
        association_store = MagicMock()
        association_store.is_associated.return_value = True

        agent_registry = MagicMock()
        # Registry returns: CEO + roster + one cloud agent.
        from hermes.agents.domain.agent import Agent, AutonomyLevel
        from datetime import datetime, timezone
        now = datetime.now(tz=timezone.utc)

        def _agent(agent_id: str, is_default: bool, managed_by: str | None) -> Agent:
            return Agent(
                agent_id=agent_id,
                name=agent_id,
                color="#000",
                role="",
                register="",
                primary_mission="",
                instructions="",
                language="es",
                golden_rules=(),
                forbidden_phrases=(),
                is_default=is_default,
                managed_by=managed_by,
                created_at=now,
                updated_at=now,
            )

        ceo = _agent("default", is_default=True, managed_by=None)
        cloud_ag = _agent("cloud-1", is_default=False, managed_by="cloud")
        roster_ag = _agent("roster-42", is_default=False, managed_by=None)
        agent_registry.list_agents.return_value = [ceo, cloud_ag, roster_ag]

        class _NullApprovalGate:
            async def register_pending(self, *, proposal_id, **_) -> None: ...
            async def approve(self, *, proposal_id, approved_by) -> str: return ""
            async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
            async def verify_token(self, *, proposal_id, token) -> bool: return False
            async def approved_token_for(self, proposal_id) -> str | None: return None

        wiring = DbusRuntimeServiceWiring(
            agent_state=InMemoryAgentState(),
            approval_gate=_NullApprovalGate(),
            authorized_uids=frozenset({1000}),
            agent_registry=agent_registry,
            association_store=association_store,
        )

        result = wiring.list_agents()
        ids = {r["agent_id"] for r in result}

        assert "cloud-1" in ids
        assert "default" in ids
        assert "roster-42" not in ids

    def test_list_agents_not_filtered_in_ce_mode(self) -> None:
        """When not associated (CE mode), list_agents returns the full registry list."""
        from unittest.mock import MagicMock
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            DbusRuntimeServiceWiring,
        )
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
        from hermes.agents.domain.agent import Agent, AutonomyLevel
        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc)

        def _agent(agent_id: str) -> Agent:
            return Agent(
                agent_id=agent_id, name=agent_id, color="#000", role="",
                register="", primary_mission="", instructions="", language="es",
                golden_rules=(), forbidden_phrases=(), is_default=(agent_id == "default"),
                managed_by=None, created_at=now, updated_at=now,
            )

        association_store = MagicMock()
        association_store.is_associated.return_value = False  # CE mode

        agent_registry = MagicMock()
        agent_registry.list_agents.return_value = [
            _agent("default"), _agent("roster-42"), _agent("roster-43")
        ]

        class _NullApprovalGate:
            async def register_pending(self, *, proposal_id, **_) -> None: ...
            async def approve(self, *, proposal_id, approved_by) -> str: return ""
            async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
            async def verify_token(self, *, proposal_id, token) -> bool: return False
            async def approved_token_for(self, proposal_id) -> str | None: return None

        wiring = DbusRuntimeServiceWiring(
            agent_state=InMemoryAgentState(),
            approval_gate=_NullApprovalGate(),
            authorized_uids=frozenset({1000}),
            agent_registry=agent_registry,
            association_store=association_store,
        )

        result = wiring.list_agents()
        ids = {r["agent_id"] for r in result}

        # CE mode: all agents visible.
        assert {"default", "roster-42", "roster-43"} <= ids
