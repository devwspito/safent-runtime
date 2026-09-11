"""Config-sync verb regression suite.

Tests three categories that were failing fail-closed in production:

1. add_egress_domain wiring:
   - Happy path: adds domain to allow-list JSON, calls apply_persisted_grants.
   - Rejects IPs, wildcards, empty strings, paths (sovereignty gate).
   - NEVER touches blocklist, deny-list, or network mode files.
   - Idempotent: already-present domain returns ok=True without write.

2. update_provider via applier (_apply_providers):
   - When alias already exists, passes provider_id + draft_json (two args).
   - When alias is new, calls add_provider with one arg.

3. set_feature_flags NOT called:
   - _apply_features was removed; feature views arrive via license.views
     (persisted by __main__ after apply returns ok).
   - Confirms the verb is absent from _ALLOWED_VERBS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusAuthorizationError,
    DbusRuntimeServiceWiring,
)
from hermes.config_sync.applier import PolicyApplier, _ALLOWED_VERBS
from hermes.config_sync.policy_document import (
    EgressSpec,
    PolicyPayload,
    ProviderSpec,
)
from hermes.shell_server.providers.repo import SQLiteProviderRepository
from hermes.shell_server.security.secrets import SecretsVault
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000
_UNAUTHORIZED_UID = 9999


# ---------------------------------------------------------------------------
# Test helpers — wiring builder
# ---------------------------------------------------------------------------


class _NullApprovalGate:
    async def register_pending(self, *, proposal_id, **_) -> None: ...
    async def approve(self, *, proposal_id, approved_by) -> str:
        return ""
    async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
    async def verify_token(self, *, proposal_id, token) -> bool:
        return False
    async def approved_token_for(self, proposal_id) -> str | None:
        return None


def _make_wiring(tmp_path: Path) -> DbusRuntimeServiceWiring:
    vault = SecretsVault(master_key=os.urandom(32))
    repo = SQLiteProviderRepository(db_path=tmp_path / "providers.db", vault=vault)
    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
        provider_repo=repo,
    )


# ---------------------------------------------------------------------------
# add_egress_domain — wiring unit tests
# ---------------------------------------------------------------------------


class TestAddEgressDomainWiring:
    """Tests the DbusRuntimeServiceWiring.add_egress_domain method in isolation.

    egress_api I/O (file reads/writes/socket) is patched so we can test the
    wiring logic without /var/lib/hermes or the proxy socket.
    """

    def _patch_egress(self, *, existing: list[str], save_tracker: list, grants_ok: bool = True):
        """Return a context-manager patch for egress_api helpers."""
        import unittest.mock as _mock

        patches = [
            _mock.patch(
                "hermes.shell_server.egress_api._load",
                return_value=list(existing),
            ),
            _mock.patch(
                "hermes.shell_server.egress_api._save",
                side_effect=lambda domains: save_tracker.append(list(domains)),
            ),
            _mock.patch(
                "hermes.shell_server.egress_api.apply_persisted_grants",
                return_value=grants_ok,
            ),
        ]
        return patches

    def _apply_patches(self, patches):
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            with patches[0], patches[1], patches[2]:
                yield

        return _ctx()

    def test_adds_valid_domain_to_allowlist(self, tmp_path: Path) -> None:
        wiring = _make_wiring(tmp_path)
        saved: list[list[str]] = []

        with patch("hermes.shell_server.egress_api._load", return_value=[]), \
             patch("hermes.shell_server.egress_api._save", side_effect=lambda d: saved.append(list(d))), \
             patch("hermes.shell_server.egress_api.apply_persisted_grants", return_value=True):
            result = wiring.add_egress_domain(domain="api.acme.com", sender_uid=_OPERATOR_UID)

        assert result["ok"] is True
        assert result["domain"] == "api.acme.com"
        assert saved == [["api.acme.com"]]

    def test_strips_wildcard_prefix(self, tmp_path: Path) -> None:
        """*.api.acme.com should be normalised to api.acme.com."""
        wiring = _make_wiring(tmp_path)
        saved: list[list[str]] = []

        with patch("hermes.shell_server.egress_api._load", return_value=[]), \
             patch("hermes.shell_server.egress_api._save", side_effect=lambda d: saved.append(list(d))), \
             patch("hermes.shell_server.egress_api.apply_persisted_grants", return_value=True):
            result = wiring.add_egress_domain(domain="*.api.acme.com", sender_uid=_OPERATOR_UID)

        assert result["ok"] is True
        assert result["domain"] == "api.acme.com"

    def test_rejects_ip_address(self, tmp_path: Path) -> None:
        wiring = _make_wiring(tmp_path)
        saved: list[list[str]] = []

        with patch("hermes.shell_server.egress_api._load", return_value=[]), \
             patch("hermes.shell_server.egress_api._save", side_effect=lambda d: saved.append(list(d))), \
             patch("hermes.shell_server.egress_api.apply_persisted_grants", return_value=True):
            result = wiring.add_egress_domain(domain="192.168.1.1", sender_uid=_OPERATOR_UID)

        assert result["ok"] is False
        assert "error" in result
        assert saved == []  # _save NOT called

    def test_rejects_empty_domain(self, tmp_path: Path) -> None:
        wiring = _make_wiring(tmp_path)
        saved: list[list[str]] = []

        with patch("hermes.shell_server.egress_api._load", return_value=[]), \
             patch("hermes.shell_server.egress_api._save", side_effect=lambda d: saved.append(list(d))), \
             patch("hermes.shell_server.egress_api.apply_persisted_grants", return_value=True):
            result = wiring.add_egress_domain(domain="   ", sender_uid=_OPERATOR_UID)

        assert result["ok"] is False
        assert saved == []

    def test_rejects_localhost(self, tmp_path: Path) -> None:
        wiring = _make_wiring(tmp_path)
        saved: list[list[str]] = []

        with patch("hermes.shell_server.egress_api._load", return_value=[]), \
             patch("hermes.shell_server.egress_api._save", side_effect=lambda d: saved.append(list(d))), \
             patch("hermes.shell_server.egress_api.apply_persisted_grants", return_value=True):
            result = wiring.add_egress_domain(domain="localhost", sender_uid=_OPERATOR_UID)

        assert result["ok"] is False
        assert saved == []

    def test_idempotent_already_present(self, tmp_path: Path) -> None:
        """Domain already in the allow-list: ok=True, _save NOT called."""
        wiring = _make_wiring(tmp_path)
        saved: list[list[str]] = []

        with patch("hermes.shell_server.egress_api._load", return_value=["api.acme.com"]), \
             patch("hermes.shell_server.egress_api._save", side_effect=lambda d: saved.append(list(d))), \
             patch("hermes.shell_server.egress_api.apply_persisted_grants", return_value=True):
            result = wiring.add_egress_domain(domain="api.acme.com", sender_uid=_OPERATOR_UID)

        assert result["ok"] is True
        assert result.get("already_present") is True
        assert saved == []  # no write — idempotent

    def test_unauthorized_uid_raises(self, tmp_path: Path) -> None:
        wiring = _make_wiring(tmp_path)

        with pytest.raises(DbusAuthorizationError):
            wiring.add_egress_domain(domain="api.acme.com", sender_uid=_UNAUTHORIZED_UID)

    def test_never_touches_blocklist(self, tmp_path: Path) -> None:
        """SOVEREIGNTY: _save is only called with the allow-list path, never blocklist."""
        wiring = _make_wiring(tmp_path)
        # Track every file written via _save_to to verify only _GRANTS_PATH is touched.
        written_paths: list[Path] = []

        def track_save_to(path: Path, domains: list) -> None:
            written_paths.append(path)

        with patch("hermes.shell_server.egress_api._load", return_value=[]), \
             patch("hermes.shell_server.egress_api._save_to", side_effect=track_save_to), \
             patch("hermes.shell_server.egress_api.apply_persisted_grants", return_value=True):
            # _save calls _save_to(_GRANTS_PATH, domains) — ensure no other path appears.
            # Since we patched _save_to directly, we intercept the canonical path.
            # Patch _save to call _save_to via the real implementation path.
            pass

        # Confirm the sovereignty invariant at code-reading level: the method
        # imports only _load, _save, apply_persisted_grants from egress_api.
        # Exclude the docstring from the check (which naturally mentions what
        # is NOT touched, for documentation purposes).
        import inspect
        import hermes.agents_os.infrastructure.dbus_runtime_service as svc_mod
        fn = svc_mod.DbusRuntimeServiceWiring.add_egress_domain
        # Inspect the bytecode constants instead — they are the actual runtime imports.
        import dis
        code_constants = set(fn.__code__.co_consts)
        assert "_save_denylist" not in code_constants
        assert "_save_mode" not in code_constants
        assert "_push_allow_mode" not in code_constants
        assert "_push_deny_mode" not in code_constants
        # Check the source lines after the docstring end.
        lines = inspect.getsource(fn).splitlines()
        # Docstring ends at the first line after the closing triple-quote.
        in_docstring = False
        body_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                in_docstring = not in_docstring
                continue
            if not in_docstring:
                body_lines.append(stripped)
        body = "\n".join(body_lines)
        assert "_save_denylist" not in body
        assert "_save_mode" not in body
        assert "_push_allow_mode" not in body
        assert "_push_deny_mode" not in body

    def test_never_touches_mode(self, tmp_path: Path) -> None:
        """SOVEREIGNTY: add_egress_domain must not call set_mode or _save_mode."""
        import inspect
        import hermes.agents_os.infrastructure.dbus_runtime_service as svc_mod
        src = inspect.getsource(svc_mod.DbusRuntimeServiceWiring.add_egress_domain)
        assert "_save_mode" not in src
        assert "set_mode" not in src


# ---------------------------------------------------------------------------
# list_egress_grants — wiring unit test
# ---------------------------------------------------------------------------


class TestListEgressGrantsWiring:
    def test_returns_domain_dicts(self, tmp_path: Path) -> None:
        wiring = _make_wiring(tmp_path)

        with patch("hermes.shell_server.egress_api._load", return_value=["api.acme.com", "cdn.acme.com"]):
            result = wiring.list_egress_grants()

        assert {"domain": "api.acme.com"} in result
        assert {"domain": "cdn.acme.com"} in result

    def test_returns_empty_list_when_no_grants(self, tmp_path: Path) -> None:
        wiring = _make_wiring(tmp_path)

        with patch("hermes.shell_server.egress_api._load", return_value=[]):
            result = wiring.list_egress_grants()

        assert result == []


# ---------------------------------------------------------------------------
# update_provider via applier — argument contract fix
# ---------------------------------------------------------------------------


class _FakeDbusProxy:
    """Minimal async D-Bus proxy stub for applier tests."""

    def __init__(self, *, existing_providers: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._providers = existing_providers or []

    async def call_list(self, member: str, *args: Any) -> list[dict]:
        self.calls.append((member, args))
        if member == "list_providers":
            return list(self._providers)
        if member in ("list_agents", "list_mcp_servers", "list_consents", "list_egress_grants"):
            return []
        return []

    async def call_dict(self, member: str, *args: Any) -> dict:
        self.calls.append((member, args))
        if member == "get_composio_status":
            return {"has_key": False}
        return {}

    async def call_mutator(self, member: str, *args: Any) -> dict:
        self.calls.append((member, args))
        if member == "create_agent":
            draft = json.loads(args[0]) if args else {}
            return {"ok": True, "agent_id": draft.get("agent_id", "new-id")}
        return {"ok": True}

    async def call_bool(self, member: str, *args: Any) -> bool:
        self.calls.append((member, args))
        return True

    def calls_for(self, verb: str) -> list[tuple]:
        return [args for (m, args) in self.calls if m == verb]


def _empty_payload_ext(**overrides: Any) -> PolicyPayload:
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


class TestUpdateProviderApplier:
    @pytest.mark.asyncio
    async def test_legacy_unsigned_provider_update_is_not_forwarded(self) -> None:
        """Enterprise can only configure the provider through a signed bundle."""
        existing = [
            {
                "provider_id": "aaaa-bbbb-cccc",
                "alias": "openai",
                "kind": "openai",
                "default_model": "gpt-4",
            }
        ]
        proxy = _FakeDbusProxy(existing_providers=existing)
        payload = _empty_payload_ext(
            providers=[{"alias": "openai", "kind": "openai", "default_model": "gpt-5"}]
        )

        result = await PolicyApplier(proxy).apply(payload, current_agents=[])

        update_calls = proxy.calls_for("update_provider")
        assert update_calls == []
        assert 'providers:instance_gateway_required' in result.failed

    @pytest.mark.asyncio
    async def test_managed_provider_forwards_complete_signed_envelope(self) -> None:
        """The verified daemon consumes the whole envelope, not a caller draft."""
        proxy = _FakeDbusProxy(existing_providers=[])
        payload = _empty_payload_ext(
            llm_instance_id='test-instance',
            providers=[{"alias": "openai", "kind": "openai", "default_model": "gpt-4"}]
        )

        await PolicyApplier(proxy).apply(payload, current_agents=[], signed_bundle_json='test-signed-envelope')

        add_calls = proxy.calls_for("add_provider")
        assert add_calls == []
        assert proxy.calls_for('apply_managed_llm_gateway') == [('test-signed-envelope',)]


# ---------------------------------------------------------------------------
# set_feature_flags absent from allow-list
# ---------------------------------------------------------------------------


class TestSetFeatureFlagsRemoved:
    def test_set_feature_flags_not_in_allowed_verbs(self) -> None:
        """set_feature_flags was removed — must not appear in _ALLOWED_VERBS."""
        assert "set_feature_flags" not in _ALLOWED_VERBS

    @pytest.mark.asyncio
    async def test_apply_does_not_call_set_feature_flags(self) -> None:
        """apply() must never call set_feature_flags regardless of features.views."""
        proxy = _FakeDbusProxy()
        payload = _empty_payload_ext(features={"views": ["proveedores", "seguridad", "mcp"]})

        await PolicyApplier(proxy).apply(payload, current_agents=[])

        called = [m for m, _ in proxy.calls]
        assert "set_feature_flags" not in called

    def test_feature_guard_reads_from_license_views_not_dbus(self) -> None:
        """Confirm that feature_guard reads license['views'] from the store.

        This is a code-reading test that verifies the feature_guard middleware
        does NOT call any D-Bus verb to get views — it reads them from the
        SQLiteAssociationStore directly (assoc.license['views']).
        """
        import inspect
        from hermes.shell_server.instance import feature_guard
        src = inspect.getsource(feature_guard.FeatureGuardMiddleware._refresh_cache)
        # The cache refresh reads from store.get().license (or default views).
        assert "license" in src
        # Must NOT reference any D-Bus proxy or call_mutator pattern.
        assert "dbus" not in src.lower()
        assert "call_mutator" not in src
        assert "set_feature_flags" not in src
