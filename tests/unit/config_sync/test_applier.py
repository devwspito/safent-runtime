"""Tests for PolicyApplier — uses FakeDbusProxy; no D-Bus bus required."""

from __future__ import annotations

from typing import Any

import pytest

from hermes.config_sync.applier import (
    ApplyResult,
    PolicyApplier,
    _is_ok_lenient,
    _is_ok_strict,
    _is_permanent_rejection,
    _is_safe_base_url,
)
from hermes.config_sync.policy_document import (
    AgentSpec,
    ConsentSpec,
    DirectorySpec,
    EgressSpec,
    FeaturesSpec,
    IntegrationSpec,
    LicenseSpec,
    McpSpec,
    PolicyPayload,
    ProviderSpec,
    SkillSpec,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# FakeDbusProxy
# ---------------------------------------------------------------------------


class FakeDbusProxy:
    """Records every D-Bus call; defaults to ok responses.

    Supports call_dict (needed by _apply_integrations for get_composio_status).
    """

    def __init__(self, *, existing_agents: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._existing_agents: list[dict] = existing_agents or []
        self._existing_providers: list[dict] = []
        self._existing_mcp: list[dict] = []
        self._existing_consents: list[dict] = []
        self._existing_egress: list[dict] = []
        # Composio status returned by call_dict("get_composio_status")
        self._composio_status: dict = {"has_key": False}
        # verb → return failure
        self._fail_verbs: set[str] = set()

    def fail_verb(self, verb: str) -> None:
        self._fail_verbs.add(verb)

    def set_composio_status(self, status: dict) -> None:
        self._composio_status = status

    async def call_list(self, member: str, *args: Any) -> list[dict]:
        self.calls.append((member, args))
        if member == "list_agents":
            return list(self._existing_agents)
        if member == "list_providers":
            return list(self._existing_providers)
        if member == "list_mcp_servers":
            return list(self._existing_mcp)
        if member == "list_consents":
            return list(self._existing_consents)
        if member == "list_egress_grants":
            return list(self._existing_egress)
        return []

    async def call_dict(self, member: str, *args: Any) -> dict:
        self.calls.append((member, args))
        if member == "get_composio_status":
            return dict(self._composio_status)
        return {}

    async def call_mutator(self, member: str, *args: Any) -> dict:
        self.calls.append((member, args))
        if member in self._fail_verbs:
            return {"ok": False, "error": "injected_failure"}
        if member == "create_agent":
            import json  # noqa: PLC0415
            draft = json.loads(args[0]) if args else {}
            return {"ok": True, "agent_id": draft.get("agent_id", "new-id"), "id": draft.get("agent_id", "new-id")}
        return {"ok": True}

    async def call_bool(self, member: str, *args: Any) -> bool:
        self.calls.append((member, args))
        if member in self._fail_verbs:
            return False
        return True

    def called_verbs(self) -> list[str]:
        return [verb for verb, _ in self.calls]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_payload(**overrides: Any) -> PolicyPayload:
    data: dict = {
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
    data.update(overrides)
    return PolicyPayload.model_validate(data)


# ---------------------------------------------------------------------------
# Section application order
# ---------------------------------------------------------------------------


class TestApplicationOrder:
    @pytest.mark.asyncio
    async def test_section_order(self) -> None:
        proxy = FakeDbusProxy()
        payload = _empty_payload(
            providers=[{"alias": "openai", "kind": "openai", "default_model": "gpt-4"}],
            integrations=[{"kind": "composio", "api_key": "key123"}],
            mcp=[{"server_id": "mcp1", "argv": ["npx", "mcp1"]}],
            skills=[{"identifier": "web-search"}],
            agents=[{"agent_id": "a1", "name": "Support"}],
            consents=[{"capability": "browser_navigate", "scope": "session"}],
            egress={"allow_domains": ["api.example.com"]},
        )

        applier = PolicyApplier(proxy)
        await applier.apply(payload, current_agents=[])

        verbs = proxy.called_verbs()
        assert verbs.index("add_provider") < verbs.index("create_agent")
        assert verbs.index("add_mcp_server") < verbs.index("create_agent")
        assert verbs.index("set_composio_api_key") < verbs.index("create_agent")
        last_agent_idx = max(i for i, v in enumerate(verbs) if v == "create_agent")
        first_consent_idx = verbs.index("grant_consent")
        assert last_agent_idx < first_consent_idx


# ---------------------------------------------------------------------------
# P0-3: D-Bus verb allowlist
# ---------------------------------------------------------------------------


class TestVerbAllowlist:
    @pytest.mark.asyncio
    async def test_unlisted_verb_is_not_called_and_logged_as_failure(self) -> None:
        """_call_mutator must refuse any verb not in _ALLOWED_VERBS."""
        from hermes.config_sync.applier import _ALLOWED_VERBS

        proxy = FakeDbusProxy()
        applier = PolicyApplier(proxy)

        # Call a verb that is certainly not in the allowlist.
        result = await applier._call_mutator("delete_all_state")

        assert "delete_all_state" not in proxy.called_verbs()
        assert result == {"ok": False, "error": "verb_not_in_allowlist"}

    @pytest.mark.asyncio
    async def test_allowlist_does_not_include_dangerous_mode_verbs(self) -> None:
        from hermes.config_sync.applier import _ALLOWED_VERBS

        for dangerous in ("set_egress_mode", "disable_blocklist", "set_network_policy"):
            assert dangerous not in _ALLOWED_VERBS

    @pytest.mark.asyncio
    async def test_allowed_verb_is_passed_through(self) -> None:
        proxy = FakeDbusProxy()
        applier = PolicyApplier(proxy)
        result = await applier._call_mutator("add_provider", '{"alias":"test"}')
        assert result.get("ok") is True


# ---------------------------------------------------------------------------
# P0-3: Egress domain validation
# ---------------------------------------------------------------------------


class TestEgressDomainValidation:
    @pytest.mark.asyncio
    async def test_ip_address_domain_rejected(self) -> None:
        proxy = FakeDbusProxy()
        payload = _empty_payload(egress={"allow_domains": ["192.168.1.1"]})
        result = await PolicyApplier(proxy).apply(payload, current_agents=[])
        assert "add_egress_domain" not in proxy.called_verbs()
        assert any("192.168.1.1" in f for f in result.failed)

    @pytest.mark.asyncio
    async def test_wildcard_prefix_stripped_and_validated(self) -> None:
        """*.example.com should be treated as example.com after stripping wildcard."""
        proxy = FakeDbusProxy()
        payload = _empty_payload(egress={"allow_domains": ["*.api.example.com"]})
        await PolicyApplier(proxy).apply(payload, current_agents=[])
        # wildcard-stripped domain api.example.com is valid — should be added
        assert "add_egress_domain" in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_empty_domain_string_rejected(self) -> None:
        proxy = FakeDbusProxy()
        payload = _empty_payload(egress={"allow_domains": ["  "]})
        result = await PolicyApplier(proxy).apply(payload, current_agents=[])
        assert "add_egress_domain" not in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_localhost_domain_rejected(self) -> None:
        proxy = FakeDbusProxy()
        # "localhost" does not match _DOMAIN_RE (no TLD)
        payload = _empty_payload(egress={"allow_domains": ["localhost"]})
        result = await PolicyApplier(proxy).apply(payload, current_agents=[])
        assert "add_egress_domain" not in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_valid_domain_accepted(self) -> None:
        proxy = FakeDbusProxy()
        payload = _empty_payload(egress={"allow_domains": ["api.acme.com"]})
        await PolicyApplier(proxy).apply(payload, current_agents=[])
        assert "add_egress_domain" in proxy.called_verbs()


# ---------------------------------------------------------------------------
# Agent upsert + delete (declarative reconcile)
# ---------------------------------------------------------------------------


class TestProviderReconcile:
    @pytest.mark.asyncio
    async def test_provider_draft_stamps_managed_by_cloud(self) -> None:
        import json  # noqa: PLC0415

        proxy = FakeDbusProxy()
        payload = _empty_payload(
            providers=[{"alias": "openai", "kind": "openai", "default_model": "gpt-4"}]
        )
        await PolicyApplier(proxy).apply(payload, current_agents=[])
        add_calls = [(v, args) for v, args in proxy.calls if v == "add_provider"]
        assert len(add_calls) == 1
        draft = json.loads(add_calls[0][1][0])
        assert draft["managed_by"] == "cloud"

    @pytest.mark.asyncio
    async def test_deletes_cloud_managed_provider_absent_from_bundle(self) -> None:
        proxy = FakeDbusProxy()
        proxy._existing_providers = [
            {"provider_id": "stale", "alias": "old-vllm", "managed_by": "cloud"}
        ]
        payload = _empty_payload(providers=[])
        await PolicyApplier(proxy).apply(payload, current_agents=[])
        assert "delete_provider" in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_does_not_delete_local_provider(self) -> None:
        proxy = FakeDbusProxy()
        proxy._existing_providers = [
            {"provider_id": "mine", "alias": "my-ollama", "managed_by": None}
        ]
        payload = _empty_payload(providers=[])
        await PolicyApplier(proxy).apply(payload, current_agents=[])
        assert "delete_provider" not in proxy.called_verbs()


class TestAgentReconcile:
    @pytest.mark.asyncio
    async def test_creates_new_cloud_agent(self) -> None:
        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(agents=[{"agent_id": "cloud-1", "name": "Cloud Agent"}])

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "create_agent" in proxy.called_verbs()
        assert result.ok

    @pytest.mark.asyncio
    async def test_updates_existing_cloud_agent(self) -> None:
        existing = [{"agent_id": "cloud-1", "name": "Old Name", "managed_by": "cloud"}]
        proxy = FakeDbusProxy(existing_agents=existing)
        payload = _empty_payload(agents=[{"agent_id": "cloud-1", "name": "New Name"}])

        await PolicyApplier(proxy).apply(payload, current_agents=existing)

        assert "update_agent" in proxy.called_verbs()
        assert "create_agent" not in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_deletes_cloud_managed_agent_absent_from_bundle(self) -> None:
        existing = [{"agent_id": "stale-cloud", "name": "Old", "managed_by": "cloud"}]
        proxy = FakeDbusProxy(existing_agents=existing)
        payload = _empty_payload(agents=[])

        await PolicyApplier(proxy).apply(payload, current_agents=existing)

        assert "delete_agent" in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_does_not_delete_locally_created_agent(self) -> None:
        existing = [{"agent_id": "local-agent", "name": "Mine", "managed_by": None}]
        proxy = FakeDbusProxy(existing_agents=existing)
        payload = _empty_payload(agents=[])

        await PolicyApplier(proxy).apply(payload, current_agents=existing)

        assert "delete_agent" not in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_provider_alias_propagated_in_agent_draft(self) -> None:
        import json  # noqa: PLC0415

        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(
            agents=[{"agent_id": "a1", "name": "Sales", "provider_alias": "anthropic-claude"}]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        create_calls = [(v, args) for v, args in proxy.calls if v == "create_agent"]
        assert len(create_calls) == 1
        draft = json.loads(create_calls[0][1][0])
        assert draft["provider_alias"] == "anthropic-claude"

    @pytest.mark.asyncio
    async def test_capability_binding_called_after_create(self) -> None:
        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(
            agents=[
                {
                    "agent_id": "a1",
                    "name": "Sales",
                    "capabilities": [{"kind": "skill", "id": "web-search", "version": "1"}],
                }
            ]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "bind_capability_to_agent" in proxy.called_verbs()


# ---------------------------------------------------------------------------
# P1-4: Delete only after all upserts succeed
# ---------------------------------------------------------------------------


class TestDeleteOnlyAfterUpserts:
    @pytest.mark.asyncio
    async def test_stale_agent_not_deleted_if_upsert_phase_fails(self) -> None:
        """If a provider upsert fails, cloud-managed agents must NOT be deleted."""
        stale_agent = {"agent_id": "stale-cloud", "name": "Old", "managed_by": "cloud"}
        proxy = FakeDbusProxy(existing_agents=[stale_agent])
        proxy.fail_verb("add_provider")

        payload = _empty_payload(
            providers=[{"alias": "openai", "kind": "openai", "default_model": "gpt-4"}],
            agents=[],  # stale-cloud not in bundle → should be deleted, but upsert failed
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[stale_agent])

        # Upsert failed → delete phase must be skipped.
        assert "delete_agent" not in proxy.called_verbs()
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_stale_agent_deleted_when_all_upserts_succeed(self) -> None:
        """When all upserts succeed, stale cloud-managed agents are removed."""
        stale_agent = {"agent_id": "stale-cloud", "name": "Old", "managed_by": "cloud"}
        proxy = FakeDbusProxy(existing_agents=[stale_agent])
        payload = _empty_payload(agents=[])  # stale-cloud not in bundle

        result = await PolicyApplier(proxy).apply(payload, current_agents=[stale_agent])

        assert "delete_agent" in proxy.called_verbs()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_applying_same_bundle_twice_does_not_duplicate_agents(self) -> None:
        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(agents=[{"agent_id": "a1", "name": "Support"}])

        applier = PolicyApplier(proxy)
        await applier.apply(payload, current_agents=[])
        assert proxy.called_verbs().count("create_agent") == 1

        proxy.calls.clear()
        existing_after = [{"agent_id": "a1", "name": "Support", "managed_by": "cloud"}]
        await applier.apply(payload, current_agents=existing_after)

        assert "create_agent" not in proxy.called_verbs()
        assert "update_agent" in proxy.called_verbs()


# ---------------------------------------------------------------------------
# ok:false handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_failed_provider_recorded_not_aborted(self) -> None:
        proxy = FakeDbusProxy()
        proxy.fail_verb("add_provider")
        payload = _empty_payload(
            providers=[{"alias": "openai", "kind": "openai", "default_model": "gpt-4"}],
            agents=[{"agent_id": "a1", "name": "Sales"}],
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert any("provider:openai" in f for f in result.failed)
        assert "create_agent" in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_apply_result_ok_false_when_any_entity_fails(self) -> None:
        proxy = FakeDbusProxy()
        proxy.fail_verb("add_provider")
        payload = _empty_payload(
            providers=[{"alias": "openai", "kind": "openai", "default_model": "gpt-4"}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])
        assert result.ok is False
        assert len(result.failed) > 0

    @pytest.mark.asyncio
    async def test_mcp_ok_false_adds_to_failed(self) -> None:
        proxy = FakeDbusProxy()
        proxy.fail_verb("add_mcp_server")
        payload = _empty_payload(mcp=[{"server_id": "mcp1", "argv": ["npx", "mcp1"]}])

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])
        assert any("mcp:mcp1" in f for f in result.failed)


# ---------------------------------------------------------------------------
# P0-4: Integration key not overwritten when local key exists
# ---------------------------------------------------------------------------


class TestIntegrationKeyProtection:
    @pytest.mark.asyncio
    async def test_key_pushed_when_no_existing_key(self) -> None:
        proxy = FakeDbusProxy()
        proxy.set_composio_status({"has_key": False})
        payload = _empty_payload(integrations=[{"kind": "composio", "api_key": "new-key"}])

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "set_composio_api_key" in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_key_not_overwritten_when_local_key_exists(self) -> None:
        """P0-4: A local (non-cloud) key must not be overwritten by the cloud."""
        proxy = FakeDbusProxy()
        # has_key=True and managed_by is NOT "cloud" → local key
        proxy.set_composio_status({"has_key": True, "managed_by": "local"})
        payload = _empty_payload(integrations=[{"kind": "composio", "api_key": "cloud-key"}])

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "set_composio_api_key" not in proxy.called_verbs()
        # Counted as applied (skipped, not failed).
        assert result.ok

    @pytest.mark.asyncio
    async def test_key_overwritten_when_managed_by_cloud(self) -> None:
        """Cloud can update its own key (managed_by='cloud' means cloud owns it)."""
        proxy = FakeDbusProxy()
        proxy.set_composio_status({"has_key": True, "managed_by": "cloud"})
        payload = _empty_payload(integrations=[{"kind": "composio", "api_key": "new-cloud-key"}])

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "set_composio_api_key" in proxy.called_verbs()


# ---------------------------------------------------------------------------
# High-risk consents
# ---------------------------------------------------------------------------


class TestHighRiskConsents:
    @pytest.mark.asyncio
    async def test_terminal_exec_consent_not_granted(self) -> None:
        """High-risk consents must be classified as pending_operator (rejected),
        NOT as transitory failures — they must not block version advancement."""
        proxy = FakeDbusProxy()
        payload = _empty_payload(consents=[{"capability": "terminal_exec", "scope": "session"}])

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "grant_consent" not in proxy.called_verbs()
        # High-risk consents go to rejected (pending_operator), NOT failed.
        assert any("terminal_exec" in r for r in result.rejected)
        assert not any("terminal_exec" in f for f in result.failed)
        # Version can advance even with a high-risk consent pending.
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_file_write_consent_not_granted(self) -> None:
        proxy = FakeDbusProxy()
        payload = _empty_payload(consents=[{"capability": "file_write", "scope": "permanent"}])

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "grant_consent" not in proxy.called_verbs()
        assert any("file_write" in r for r in result.rejected)
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_low_risk_consent_granted(self) -> None:
        proxy = FakeDbusProxy()
        payload = _empty_payload(consents=[{"capability": "browser_navigate", "scope": "session"}])

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "grant_consent" in proxy.called_verbs()


# ---------------------------------------------------------------------------
# Egress sovereignty invariants
# ---------------------------------------------------------------------------


class TestEgressInvariants:
    @pytest.mark.asyncio
    async def test_egress_adds_domains_only(self) -> None:
        proxy = FakeDbusProxy()
        payload = _empty_payload(egress={"allow_domains": ["api.example.com"]})

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        verbs = proxy.called_verbs()
        assert "add_egress_domain" in verbs
        assert "set_egress_mode" not in verbs

    @pytest.mark.asyncio
    async def test_already_granted_domain_not_re_added(self) -> None:
        proxy = FakeDbusProxy()
        proxy._existing_egress = [{"domain": "api.example.com"}]
        payload = _empty_payload(egress={"allow_domains": ["api.example.com"]})

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        add_calls = [(v, a) for v, a in proxy.calls if v == "add_egress_domain"]
        assert len(add_calls) == 0


# ---------------------------------------------------------------------------
# P2: _is_ok_strict for sensitive sections
# ---------------------------------------------------------------------------


class TestIsOkStrict:
    def test_empty_dict_is_failure(self) -> None:
        assert _is_ok_strict({}) is False

    def test_none_is_failure(self) -> None:
        assert _is_ok_strict(None) is False

    def test_explicit_true_is_success(self) -> None:
        assert _is_ok_strict({"ok": True}) is True

    def test_explicit_false_is_failure(self) -> None:
        assert _is_ok_strict({"ok": False}) is False

    def test_bool_true_is_success(self) -> None:
        assert _is_ok_strict(True) is True

    def test_bool_false_is_failure(self) -> None:
        assert _is_ok_strict(False) is False

    @pytest.mark.asyncio
    async def test_empty_dict_from_egress_counts_as_failure(self) -> None:
        """P2: egress uses _is_ok_strict; {} must not be treated as success."""
        proxy = FakeDbusProxy()
        # Override call_mutator to return {} (no "ok" key) for add_egress_domain.
        orig = proxy.call_mutator

        async def patched(member: str, *args: Any) -> dict:
            if member == "add_egress_domain":
                return {}  # missing "ok" field
            return await orig(member, *args)

        proxy.call_mutator = patched  # type: ignore[method-assign]
        payload = _empty_payload(egress={"allow_domains": ["api.example.com"]})

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert any("egress:api.example.com" in f for f in result.failed)

    @pytest.mark.asyncio
    async def test_none_from_grant_consent_counts_as_failure(self) -> None:
        """P2: consents use _is_ok_strict; None must not be treated as success."""
        proxy = FakeDbusProxy()
        orig = proxy.call_mutator

        async def patched(member: str, *args: Any) -> dict | None:
            if member == "grant_consent":
                return None
            return await orig(member, *args)

        proxy.call_mutator = patched  # type: ignore[method-assign]
        payload = _empty_payload(consents=[{"capability": "browser_navigate", "scope": "session"}])

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert any("browser_navigate" in f for f in result.failed)


# ---------------------------------------------------------------------------
# P2: _is_ok_lenient for non-sensitive sections
# ---------------------------------------------------------------------------


class TestIsOkLenient:
    def test_true_dict(self) -> None:
        assert _is_ok_lenient({"ok": True}) is True

    def test_false_dict(self) -> None:
        assert _is_ok_lenient({"ok": False}) is False

    def test_empty_dict_treated_as_ok(self) -> None:
        assert _is_ok_lenient({}) is True

    def test_none_treated_as_ok(self) -> None:
        assert _is_ok_lenient(None) is True

    def test_bool_true(self) -> None:
        assert _is_ok_lenient(True) is True

    def test_bool_false(self) -> None:
        assert _is_ok_lenient(False) is False


# ---------------------------------------------------------------------------
# _is_permanent_rejection unit tests
# ---------------------------------------------------------------------------


class TestIsPermanentRejection:
    def test_blocked_true_ok_false_is_permanent(self) -> None:
        assert _is_permanent_rejection({"ok": False, "blocked": True}) is True

    def test_blocked_true_with_verdict_is_permanent(self) -> None:
        assert _is_permanent_rejection(
            {"ok": False, "blocked": True, "verdict": "FAIL", "scan_id": "s1"}
        ) is True

    def test_ok_false_without_blocked_is_not_permanent(self) -> None:
        """Generic ok:false (daemon down) is transitory, not permanent."""
        assert _is_permanent_rejection({"ok": False}) is False

    def test_ok_false_blocked_false_is_not_permanent(self) -> None:
        assert _is_permanent_rejection({"ok": False, "blocked": False}) is False

    def test_ok_true_blocked_true_is_not_permanent(self) -> None:
        """Defensive: if blocked=True but ok=True, not a permanent rejection."""
        assert _is_permanent_rejection({"ok": True, "blocked": True}) is False

    def test_none_is_not_permanent(self) -> None:
        assert _is_permanent_rejection(None) is False

    def test_bool_false_is_not_permanent(self) -> None:
        assert _is_permanent_rejection(False) is False

    def test_empty_dict_is_not_permanent(self) -> None:
        assert _is_permanent_rejection({}) is False


# ---------------------------------------------------------------------------
# P2: SSRF check for provider base_url
# ---------------------------------------------------------------------------


class TestProviderBaseUrlSsrfCheck:
    @pytest.mark.asyncio
    async def test_private_ip_base_url_rejected(self) -> None:
        proxy = FakeDbusProxy()
        payload = _empty_payload(
            providers=[
                {
                    "alias": "internal",
                    "kind": "openai",
                    "default_model": "gpt-4",
                    "base_url": "https://192.168.1.10/v1",
                }
            ]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "add_provider" not in proxy.called_verbs()
        assert any("unsafe_base_url" in f for f in result.failed)

    @pytest.mark.asyncio
    async def test_localhost_base_url_rejected(self) -> None:
        proxy = FakeDbusProxy()
        payload = _empty_payload(
            providers=[
                {
                    "alias": "local",
                    "kind": "openai",
                    "default_model": "gpt-4",
                    "base_url": "https://localhost/v1",
                }
            ]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "add_provider" not in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_http_base_url_rejected(self) -> None:
        proxy = FakeDbusProxy()
        payload = _empty_payload(
            providers=[
                {
                    "alias": "insecure",
                    "kind": "openai",
                    "default_model": "gpt-4",
                    "base_url": "http://api.example.com/v1",
                }
            ]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "add_provider" not in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_public_https_base_url_accepted(self) -> None:
        proxy = FakeDbusProxy()
        payload = _empty_payload(
            providers=[
                {
                    "alias": "ext",
                    "kind": "openai",
                    "default_model": "gpt-4",
                    "base_url": "https://api.openai.com/v1",
                }
            ]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "add_provider" in proxy.called_verbs()

    def test_is_safe_base_url_unit(self) -> None:
        assert _is_safe_base_url("https://api.openai.com/v1") is True
        assert _is_safe_base_url("https://192.168.1.1/v1") is False
        assert _is_safe_base_url("https://10.0.0.1/v1") is False
        assert _is_safe_base_url("https://localhost/v1") is False
        assert _is_safe_base_url("http://api.openai.com/v1") is False
        assert _is_safe_base_url("https://169.254.169.254/v1") is False  # AWS metadata


# ---------------------------------------------------------------------------
# Enterprise Fase 2 Phase 3: agent.access_scope -> set_agent_access_scope
# ---------------------------------------------------------------------------


class TestAgentAccessScopeApplier:
    @pytest.mark.asyncio
    async def test_access_scope_calls_set_agent_access_scope_with_json_and_tenant(self) -> None:
        import json  # noqa: PLC0415

        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(
            agents=[
                {
                    "agent_id": "a1",
                    "name": "Sales",
                    "access_scope": {
                        "enforced": True,
                        "native_tools": ["terminal"],
                        "policy_overlay": {"send_message": {"enabled": False}},
                    },
                }
            ]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[], tenant_id="tenant-xyz")

        calls = [(v, args) for v, args in proxy.calls if v == "set_agent_access_scope"]
        assert len(calls) == 1
        agent_id, scope_json, tenant_id = calls[0][1]
        assert agent_id == "a1"
        assert tenant_id == "tenant-xyz"
        scope = json.loads(scope_json)
        assert scope["enforced"] is True
        assert scope["native_tools"] == ["terminal"]
        assert scope["policy_overlay"] == {"send_message": {"enabled": False}}

    @pytest.mark.asyncio
    async def test_no_access_scope_does_not_call_the_verb(self) -> None:
        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(agents=[{"agent_id": "a1", "name": "Sales"}])

        await PolicyApplier(proxy).apply(payload, current_agents=[], tenant_id="tenant-xyz")

        assert "set_agent_access_scope" not in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_access_scope_call_happens_after_capability_binding(self) -> None:
        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(
            agents=[
                {
                    "agent_id": "a1",
                    "name": "Sales",
                    "capabilities": [{"kind": "skill", "id": "web-search", "version": "1"}],
                    "access_scope": {"enforced": True},
                }
            ]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[], tenant_id="t")

        verbs = proxy.called_verbs()
        assert verbs.index("bind_capability_to_agent") < verbs.index("set_agent_access_scope")

    @pytest.mark.asyncio
    async def test_set_agent_access_scope_failure_marks_agent_failed(self) -> None:
        proxy = FakeDbusProxy(existing_agents=[])
        proxy.fail_verb("set_agent_access_scope")
        payload = _empty_payload(
            agents=[{"agent_id": "a1", "name": "Sales", "access_scope": {"enforced": True}}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[], tenant_id="t")

        assert any("agent:a1" in f for f in result.failed)

    @pytest.mark.asyncio
    async def test_set_agent_access_scope_in_allowlist(self) -> None:
        from hermes.config_sync.applier import _ALLOWED_VERBS

        assert "set_agent_access_scope" in _ALLOWED_VERBS

    @pytest.mark.asyncio
    async def test_clear_agent_access_scope_not_in_allowlist_until_implemented(
        self,
    ) -> None:
        """F2 review fix: clear_agent_access_scope has no wiring method nor
        D-Bus export — an allow-listed-but-unreachable verb is its own bug
        class. Must stay OUT of the allow-list until both ends exist."""
        from hermes.config_sync.applier import _ALLOWED_VERBS

        assert "clear_agent_access_scope" not in _ALLOWED_VERBS


# ---------------------------------------------------------------------------
# 2026-07-07 confused-deputy fix: MCP capabilities travel via AgentAccessScope,
# NOT via bind_capability_to_agent (D-Bus-denied for config-sync's uid).
# ---------------------------------------------------------------------------


class TestMcpCapabilityViaAccessScope:
    @pytest.mark.asyncio
    async def test_mcp_capability_not_bound_via_bind_capability_to_agent(self) -> None:
        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(
            agents=[
                {
                    "agent_id": "cerebro",
                    "name": "CEO",
                    "capabilities": [
                        {"kind": "mcp", "id": "safent-control", "version": "1"}
                    ],
                    "access_scope": {"enforced": True, "cerebro_unrestricted": False},
                }
            ]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[], tenant_id="t")

        assert "bind_capability_to_agent" not in proxy.called_verbs()
        assert result.ok

    @pytest.mark.asyncio
    async def test_mcp_capability_ids_land_in_access_scope_json(self) -> None:
        import json  # noqa: PLC0415

        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(
            agents=[
                {
                    "agent_id": "cerebro",
                    "name": "CEO",
                    "capabilities": [
                        {"kind": "mcp", "id": "safent-control", "version": "1"},
                        {"kind": "mcp", "id": "other-mcp", "version": "1"},
                    ],
                    "access_scope": {"enforced": True, "cerebro_unrestricted": False},
                }
            ]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[], tenant_id="t")

        calls = [(v, args) for v, args in proxy.calls if v == "set_agent_access_scope"]
        assert len(calls) == 1
        scope = json.loads(calls[0][1][1])
        assert scope["mcp_servers"] == ["other-mcp", "safent-control"]

    @pytest.mark.asyncio
    async def test_non_mcp_capability_still_bound_via_bind_capability_to_agent(
        self,
    ) -> None:
        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(
            agents=[
                {
                    "agent_id": "a1",
                    "name": "Sales",
                    "capabilities": [
                        {"kind": "mcp", "id": "safent-control", "version": "1"},
                        {"kind": "skill", "id": "web-search", "version": "1"},
                    ],
                }
            ]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        bind_calls = [args for verb, args in proxy.calls if verb == "bind_capability_to_agent"]
        assert len(bind_calls) == 1
        assert bind_calls[0][1:3] == ("skill", "web-search")

    @pytest.mark.asyncio
    async def test_bind_capability_failure_does_not_fail_agent_apply(self) -> None:
        """bind_capability_to_agent is D-Bus-denied for config-sync's uid in
        production — its failure must never block last_applied_version."""
        proxy = FakeDbusProxy(existing_agents=[])
        proxy.fail_verb("bind_capability_to_agent")
        payload = _empty_payload(
            agents=[
                {
                    "agent_id": "a1",
                    "name": "Sales",
                    "capabilities": [{"kind": "skill", "id": "web-search", "version": "1"}],
                }
            ]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert result.ok
        assert not result.failed

    @pytest.mark.asyncio
    async def test_mcp_capabilities_without_access_scope_do_not_fail_apply(self) -> None:
        """No access_scope in the bundle → nowhere allowed to land the MCP
        authorization this sync; logged, but must not block version advancement."""
        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(
            agents=[
                {
                    "agent_id": "a1",
                    "name": "Sales",
                    "capabilities": [
                        {"kind": "mcp", "id": "safent-control", "version": "1"}
                    ],
                }
            ]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert result.ok
        assert "set_agent_access_scope" not in proxy.called_verbs()


# ---------------------------------------------------------------------------
# ApplyResult
# ---------------------------------------------------------------------------


class TestApplyResult:
    def test_ok_true_when_no_failures(self) -> None:
        r = ApplyResult(applied=3, failed=[])
        assert r.ok is True

    def test_ok_false_when_failures(self) -> None:
        r = ApplyResult(applied=2, failed=["provider:openai"])
        assert r.ok is False

    def test_ok_true_when_only_rejections_no_failures(self) -> None:
        """Permanent rejections alone must NOT block version advancement."""
        r = ApplyResult(applied=2, failed=[], rejected=["skill:bad-skill:scan_blocked"])
        assert r.ok is True

    def test_rejected_list_independent_of_ok(self) -> None:
        r = ApplyResult(applied=0, failed=[], rejected=["skill:x:scan_blocked"])
        assert r.ok is True
        assert len(r.rejected) == 1


# ---------------------------------------------------------------------------
# Fase 3 — directory (department-scoped visibility)
# ---------------------------------------------------------------------------


class FakeDirectoryStore:
    """Records update_directory() calls; satisfies _DirectoryStoreProtocol."""

    def __init__(self) -> None:
        self.calls: list[dict | None] = []

    def update_directory(self, directory: dict | None) -> None:
        self.calls.append(directory)


_DIRECTORY_ENTRY = {
    "employee_id": "emp-1",
    "agent_id": "agent-1",
    "name": "Ada",
    "department": "ventas",
}


class TestDirectoryApplier:
    @pytest.mark.asyncio
    async def test_directory_present_is_stored(self) -> None:
        proxy = FakeDbusProxy()
        store = FakeDirectoryStore()
        payload = _empty_payload(directory={"entries": [_DIRECTORY_ENTRY]})

        result = await PolicyApplier(proxy, directory_store=store).apply(
            payload, current_agents=[]
        )

        assert result.ok is True
        assert store.calls == [{"entries": [_DIRECTORY_ENTRY]}]

    @pytest.mark.asyncio
    async def test_directory_absent_clears_previously_stored_one(self) -> None:
        """A subsequent bundle with directory=None must clear the store."""
        proxy = FakeDbusProxy()
        store = FakeDirectoryStore()

        await PolicyApplier(proxy, directory_store=store).apply(
            _empty_payload(directory={"entries": [_DIRECTORY_ENTRY]}),
            current_agents=[],
        )
        await PolicyApplier(proxy, directory_store=store).apply(
            _empty_payload(), current_agents=[]
        )

        assert store.calls == [{"entries": [_DIRECTORY_ENTRY]}, None]

    @pytest.mark.asyncio
    async def test_no_directory_store_injected_is_a_no_op(self) -> None:
        """Existing single-arg PolicyApplier(proxy) callers are unaffected."""
        proxy = FakeDbusProxy()
        payload = _empty_payload(directory={"entries": [_DIRECTORY_ENTRY]})

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert result.ok is True  # never raises, never marks the section failed

    @pytest.mark.asyncio
    async def test_directory_persist_failure_does_not_block_version_advancement(
        self,
    ) -> None:
        """Presentation-only data: a persistence error must not fail apply()."""
        proxy = FakeDbusProxy()

        class _BoomStore:
            def update_directory(self, directory: dict | None) -> None:
                raise RuntimeError("disk full")

        payload = _empty_payload(directory={"entries": [_DIRECTORY_ENTRY]})
        result = await PolicyApplier(proxy, directory_store=_BoomStore()).apply(
            payload, current_agents=[]
        )

        assert result.ok is True

    def test_directory_spec_parses_from_payload_dict(self) -> None:
        payload = _empty_payload(directory={"entries": [_DIRECTORY_ENTRY]})
        assert isinstance(payload.directory, DirectorySpec)
        assert payload.directory.entries[0].employee_id == "emp-1"


# ---------------------------------------------------------------------------
# FIX B: Permanent rejection vs transitory failure
# ---------------------------------------------------------------------------


class TestPermanentRejectionVsTransitory:
    """Skill blocked by Security Center scan must be classified as permanent
    rejection, not transitory failure — version can still advance."""

    @pytest.mark.asyncio
    async def test_scan_blocked_skill_goes_to_rejected_not_failed(self) -> None:
        """install_hub_skill returns {"ok": False, "blocked": True} → rejected."""
        from typing import Any

        class ScanBlockingProxy(FakeDbusProxy):
            async def call_mutator(self, member: str, *args: Any) -> dict:
                self.calls.append((member, args))
                if member == "install_hub_skill":
                    return {"ok": False, "blocked": True, "verdict": "FAIL",
                            "scan_id": "scan-123"}
                return {"ok": True}

        proxy = ScanBlockingProxy(existing_agents=[])
        payload = _empty_payload(
            skills=[{"identifier": "dangerous-skill"}],
            agents=[{"agent_id": "a1", "name": "Sales"}],
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        # Skill is recorded as permanently rejected — NOT as transitory failure.
        assert any("dangerous-skill" in r for r in result.rejected)
        assert not any("dangerous-skill" in f for f in result.failed)

    @pytest.mark.asyncio
    async def test_scan_blocked_skill_does_not_block_version_advancement(self) -> None:
        """ok must be True even when a skill is scan-blocked."""
        from typing import Any

        class ScanBlockingProxy(FakeDbusProxy):
            async def call_mutator(self, member: str, *args: Any) -> dict:
                self.calls.append((member, args))
                if member == "install_hub_skill":
                    return {"ok": False, "blocked": True}
                return {"ok": True}

        proxy = ScanBlockingProxy(existing_agents=[])
        payload = _empty_payload(
            skills=[{"identifier": "blocked-skill"}],
            agents=[{"agent_id": "a1", "name": "Agent"}],
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert result.ok is True  # version CAN advance

    @pytest.mark.asyncio
    async def test_transitory_failure_still_blocks_version_advancement(self) -> None:
        """A generic ok:false (no blocked=True) IS a transitory failure."""
        proxy = FakeDbusProxy()
        proxy.fail_verb("install_hub_skill")  # returns {"ok": False, "error": ...}
        payload = _empty_payload(skills=[{"identifier": "some-skill"}])

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert any("some-skill" in f for f in result.failed)
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_scan_blocked_skill_with_ok_agents_still_creates_agents(self) -> None:
        """When a skill is scan-blocked, agent upserts must still proceed."""
        from typing import Any

        class ScanBlockingProxy(FakeDbusProxy):
            async def call_mutator(self, member: str, *args: Any) -> dict:
                self.calls.append((member, args))
                if member == "install_hub_skill":
                    return {"ok": False, "blocked": True}
                if member == "create_agent":
                    import json as _json  # noqa: PLC0415
                    draft = _json.loads(args[0]) if args else {}
                    return {"ok": True, "agent_id": draft.get("agent_id", "new-id")}
                return {"ok": True}

        proxy = ScanBlockingProxy(existing_agents=[])
        payload = _empty_payload(
            skills=[{"identifier": "blocked-skill"}],
            agents=[{"agent_id": "cloud-agent-1", "name": "Sales"}],
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert "create_agent" in proxy.called_verbs()
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_scan_blocked_skill_does_not_prevent_stale_agent_delete(self) -> None:
        """Scan-blocked skills are permanent rejections — stale agents MUST be deleted."""
        from typing import Any

        stale_agent = {"agent_id": "stale-cloud", "name": "Old", "managed_by": "cloud"}

        class ScanBlockingProxy(FakeDbusProxy):
            async def call_mutator(self, member: str, *args: Any) -> dict:
                self.calls.append((member, args))
                if member == "install_hub_skill":
                    return {"ok": False, "blocked": True}
                return {"ok": True}

            async def call_bool(self, member: str, *args: Any) -> bool:
                self.calls.append((member, args))
                return True

        proxy = ScanBlockingProxy(existing_agents=[stale_agent])
        # Stale agent NOT in the bundle; skill is permanently rejected.
        payload = _empty_payload(
            skills=[{"identifier": "blocked-skill"}],
            agents=[],  # stale-cloud absent → should be deleted
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[stale_agent])

        # result.ok=True so delete phase runs.
        assert result.ok is True
        assert "delete_agent" in proxy.called_verbs()

    @pytest.mark.asyncio
    async def test_transitory_failure_prevents_stale_agent_delete(self) -> None:
        """A transitory (non-blocked) failure prevents the delete phase."""
        stale_agent = {"agent_id": "stale-cloud", "name": "Old", "managed_by": "cloud"}
        proxy = FakeDbusProxy(existing_agents=[stale_agent])
        proxy.fail_verb("install_hub_skill")  # transitory — no blocked=True

        payload = _empty_payload(
            skills=[{"identifier": "some-skill"}],
            agents=[],
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[stale_agent])

        assert result.ok is False
        assert "delete_agent" not in proxy.called_verbs()


# ---------------------------------------------------------------------------
# FIX A: managed_by propagated in create_agent draft
# ---------------------------------------------------------------------------


class TestManagedByApplier:
    @pytest.mark.asyncio
    async def test_managed_by_cloud_set_on_create_agent(self) -> None:
        """New cloud agents must have managed_by='cloud' in the draft sent to the daemon."""
        import json as _json

        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(
            agents=[{"agent_id": "cloud-1", "name": "Cloud Agent",
                     "provider_alias": "openai-gpt4"}]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        create_calls = [(v, args) for v, args in proxy.calls if v == "create_agent"]
        assert len(create_calls) == 1
        draft = _json.loads(create_calls[0][1][0])
        assert draft["managed_by"] == "cloud"

    @pytest.mark.asyncio
    async def test_provider_alias_and_managed_by_both_in_draft(self) -> None:
        """Both provider_alias and managed_by must be present in the create_agent draft."""
        import json as _json

        proxy = FakeDbusProxy(existing_agents=[])
        payload = _empty_payload(
            agents=[{"agent_id": "a1", "name": "Sales",
                     "provider_alias": "anthropic-claude"}]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        create_calls = [(v, args) for v, args in proxy.calls if v == "create_agent"]
        draft = _json.loads(create_calls[0][1][0])
        assert draft["provider_alias"] == "anthropic-claude"
        assert draft["managed_by"] == "cloud"

    @pytest.mark.asyncio
    async def test_reconcile_uses_managed_by_from_list_agents(self) -> None:
        """list_agents returning managed_by='cloud' causes update (not create)."""
        existing = [{"agent_id": "cloud-1", "name": "Old", "managed_by": "cloud"}]
        proxy = FakeDbusProxy(existing_agents=existing)
        payload = _empty_payload(
            agents=[{"agent_id": "cloud-1", "name": "New Name"}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=existing)

        assert "update_agent" in proxy.called_verbs()
        assert "create_agent" not in proxy.called_verbs()
        assert result.ok


# ---------------------------------------------------------------------------
# R15: bundle-sourced MCP WARN must not permanently stall version advancement
# ---------------------------------------------------------------------------


class TestMcpBundleWarnOverride:
    """A Security-Center discretionary WARN on a bundle-sourced MCP server
    must not be classified as a transitory failure — the tenant Ed25519
    signature on the bundle is the owner's authority, so it is retried with
    the daemon's own force=True override plumbing instead of stalling
    last_applied_version forever."""

    @pytest.mark.asyncio
    async def test_warn_verdict_is_retried_with_force_and_applied(self) -> None:
        """First call WARN-blocked; retried call (force=True) succeeds → applied."""
        import json as _json
        from typing import Any

        class WarnThenForceOkProxy(FakeDbusProxy):
            async def call_mutator(self, member: str, *args: Any) -> dict:
                self.calls.append((member, args))
                if member == "add_mcp_server":
                    draft = _json.loads(args[0])
                    if draft["force"]:
                        return {"ok": True, "tool_count": 3}
                    return {
                        "ok": False, "blocked": True, "warn": True,
                        "scan_id": "scan-1", "score": 45,
                    }
                return {"ok": True}

        proxy = WarnThenForceOkProxy()
        payload = _empty_payload(
            mcp=[{"server_id": "safent-control", "argv": ["npx", "-y", "mcp-remote"]}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert result.ok is True
        assert not any("safent-control" in f for f in result.failed)
        assert not any("safent-control" in r for r in result.rejected)
        mcp_calls = [(v, args) for v, args in proxy.calls if v == "add_mcp_server"]
        assert len(mcp_calls) == 2
        assert _json.loads(mcp_calls[0][1][0])["force"] is False
        assert _json.loads(mcp_calls[1][1][0])["force"] is True

    @pytest.mark.asyncio
    async def test_first_attempt_always_uses_force_false(self) -> None:
        """_apply_mcp never starts with force=True — the bypass is earned
        only after the daemon's own scan confirms a discretionary WARN (see
        _is_discretionary_warn_block), never assumed up front. The WARN/FAIL
        gate itself (add_mcp_server, force=False) is exercised end-to-end by
        tests/unit/agents_os/test_mcp_neus_single_source.py — untouched by
        this fix; locally/agent-initiated installs still go through it."""
        import json as _json
        from typing import Any

        class RecordingProxy(FakeDbusProxy):
            async def call_mutator(self, member: str, *args: Any) -> dict:
                self.calls.append((member, args))
                return {"ok": True}

        proxy = RecordingProxy()
        payload = _empty_payload(
            mcp=[{"server_id": "safent-control", "argv": ["npx", "-y", "mcp-remote"]}]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        mcp_calls = [(v, args) for v, args in proxy.calls if v == "add_mcp_server"]
        assert len(mcp_calls) == 1
        assert _json.loads(mcp_calls[0][1][0])["force"] is False

    @pytest.mark.asyncio
    async def test_fail_verdict_on_bundle_mcp_is_rejected_not_installed(self) -> None:
        """A genuine FAIL/hard block (no 'warn' key) is NEVER force-retried —
        it is a permanent rejection: surfaced/logged, server stays
        uninstalled, but version advancement is not stalled."""
        import json as _json
        from typing import Any

        class FailBlockingProxy(FakeDbusProxy):
            async def call_mutator(self, member: str, *args: Any) -> dict:
                self.calls.append((member, args))
                if member == "add_mcp_server":
                    return {
                        "ok": False, "blocked": True, "verdict": "FAIL",
                        "scan_id": "scan-2", "score": 12,
                    }
                return {"ok": True}

        proxy = FailBlockingProxy()
        payload = _empty_payload(
            mcp=[{"server_id": "malicious-mcp", "argv": ["npx", "-y", "evil"]}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert result.ok is True  # version can still advance
        assert any("malicious-mcp" in r for r in result.rejected)
        assert not any("malicious-mcp" in f for f in result.failed)
        mcp_calls = [(v, args) for v, args in proxy.calls if v == "add_mcp_server"]
        # No force=True retry for a non-WARN block.
        assert len(mcp_calls) == 1
        assert _json.loads(mcp_calls[0][1][0])["force"] is False

    @pytest.mark.asyncio
    async def test_scanner_error_block_on_bundle_mcp_is_rejected_not_retried(self) -> None:
        """A scanner-error block (blocked=True, no 'warn' key, no 'verdict')
        is fail-closed the same way as FAIL — rejected, never force-retried."""
        from typing import Any

        class ScannerErrorProxy(FakeDbusProxy):
            async def call_mutator(self, member: str, *args: Any) -> dict:
                self.calls.append((member, args))
                if member == "add_mcp_server":
                    return {"ok": False, "blocked": True, "error": "scan failed"}
                return {"ok": True}

        proxy = ScannerErrorProxy()
        payload = _empty_payload(
            mcp=[{"server_id": "unscannable-mcp", "argv": ["npx", "-y", "x"]}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert result.ok is True
        assert any("unscannable-mcp" in r for r in result.rejected)
        mcp_calls = [(v, args) for v, args in proxy.calls if v == "add_mcp_server"]
        assert len(mcp_calls) == 1

    @pytest.mark.asyncio
    async def test_warn_still_blocked_after_force_retry_is_rejected(self) -> None:
        """Edge case: the force=True retry itself comes back blocked (e.g. the
        override could not clear it) — must be classified as a permanent
        rejection, not a transitory failure."""
        import json as _json
        from typing import Any

        class WarnAlwaysBlockedProxy(FakeDbusProxy):
            async def call_mutator(self, member: str, *args: Any) -> dict:
                self.calls.append((member, args))
                if member == "add_mcp_server":
                    return {"ok": False, "blocked": True, "warn": True, "score": 45}
                return {"ok": True}

        proxy = WarnAlwaysBlockedProxy()
        payload = _empty_payload(
            mcp=[{"server_id": "stubborn-mcp", "argv": ["npx", "-y", "x"]}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert result.ok is True
        assert any("stubborn-mcp" in r for r in result.rejected)
        mcp_calls = [(v, args) for v, args in proxy.calls if v == "add_mcp_server"]
        assert len(mcp_calls) == 2  # first attempt + force retry, both blocked

    @pytest.mark.asyncio
    async def test_existing_mcp_server_skips_scan_entirely(self) -> None:
        """Idempotency: a server already in list_mcp_servers is never re-scanned
        (no add_mcp_server call at all) — the short-circuit that keeps this
        fix from re-triggering the WARN gate on every sync tick."""
        proxy = FakeDbusProxy()
        proxy._existing_mcp = [{"server_id": "safent-control"}]
        payload = _empty_payload(
            mcp=[{"server_id": "safent-control", "argv": ["npx", "-y", "mcp-remote"]}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        assert result.ok is True
        assert "add_mcp_server" not in proxy.called_verbs()


class TestIsDiscretionaryWarnBlock:
    """Unit coverage for the classifier itself (no D-Bus/applier involved)."""

    def test_warn_block_shape_is_discretionary(self) -> None:
        from hermes.config_sync.applier import _is_discretionary_warn_block

        assert _is_discretionary_warn_block(
            {"ok": False, "blocked": True, "warn": True, "score": 45}
        ) is True

    def test_fail_block_shape_is_not_discretionary(self) -> None:
        from hermes.config_sync.applier import _is_discretionary_warn_block

        assert _is_discretionary_warn_block(
            {"ok": False, "blocked": True, "verdict": "FAIL"}
        ) is False

    def test_transitory_failure_is_not_discretionary(self) -> None:
        from hermes.config_sync.applier import _is_discretionary_warn_block

        assert _is_discretionary_warn_block({"ok": False, "error": "x"}) is False

    def test_none_and_bool_are_not_discretionary(self) -> None:
        from hermes.config_sync.applier import _is_discretionary_warn_block

        assert _is_discretionary_warn_block(None) is False
        assert _is_discretionary_warn_block(True) is False
        assert _is_discretionary_warn_block(False) is False
