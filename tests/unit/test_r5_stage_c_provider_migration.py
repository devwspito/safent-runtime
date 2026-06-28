"""R5 Stage C — Retiro del branch SQL del cascade de resolución de provider.

Cubre:
  1. Migración al arranque:
     a. Nativo vacío + SQL activo → _sync_to_native_provider llamado con set_active=True y la key revelada.
     b. Idempotencia: nativo ya poblado → no se llama _sync_to_native_provider.
     c. Fail-soft: una excepción dentro de la migración no se propaga.
  2. Cascade de resolve_model_config:
     a. load_active_model_config ya NO se invoca cuando el nativo devuelve config.
     b. Cuando el nativo es None → cae a from_env (no a SQL).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

pytestmark = pytest.mark.unit

_DBUS_MODULE = "hermes.agents_os.infrastructure.dbus_runtime_service"
_CONFIG_SOURCE_MODULE = "hermes.runtime.provider_config_source"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _NullGate:
    async def register_pending(self, *, proposal_id, **_) -> None: ...
    async def approve(self, *, proposal_id, approved_by) -> str:
        return ""
    async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
    async def verify_token(self, *, proposal_id, token) -> bool:
        return False
    async def approved_token_for(self, proposal_id) -> str | None:
        return None


def _make_wiring(*, provider_repo: Any) -> Any:
    from hermes.agents_os.infrastructure.dbus_runtime_service import (
        DbusRuntimeServiceWiring,
    )
    from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullGate(),
        authorized_uids=frozenset({1000}),
        provider_repo=provider_repo,
    )


def _make_provider(*, alias: str = "OpenAI-test", has_api_key: bool = True) -> Any:
    from hermes.shell_server.providers.domain import ProviderKind, new_provider

    return new_provider(
        alias=alias,
        kind=ProviderKind.OPENAI,
        default_model="gpt-5.4-nano",
        has_api_key=has_api_key,
    )


# ---------------------------------------------------------------------------
# 1a. Migration: native empty + SQL active → sync called with set_active=True
# ---------------------------------------------------------------------------

class TestMigrateActiveProviderToNative:

    def test_migrates_when_native_empty_and_sql_has_active(
        self, tmp_path: Path
    ) -> None:
        """When native config is empty but SQL has an active provider,
        migrate_active_provider_to_native must call _sync_to_native_provider
        with set_active=True and the revealed api_key."""
        from hermes.shell_server.providers.repo import SQLiteProviderRepository
        from hermes.shell_server.security.secrets import SecretsVault

        vault = SecretsVault(master_key=os.urandom(32))
        repo = SQLiteProviderRepository(db_path=tmp_path / "p.db", vault=vault)
        provider = _make_provider()
        repo.add(provider=provider, api_key="sk-migrate-me")
        repo.set_active(provider_id=provider.provider_id)

        wiring = _make_wiring(provider_repo=repo)

        with (
            patch(
                f"{_CONFIG_SOURCE_MODULE}._load_native_model_config",
                return_value=None,
            ),
            patch.object(wiring, "_sync_to_native_provider") as mock_sync,
        ):
            wiring.migrate_active_provider_to_native()

        mock_sync.assert_called_once()
        _, call_api_key = mock_sync.call_args[0][:2]
        assert call_api_key == "sk-migrate-me"
        assert mock_sync.call_args.kwargs.get("set_active") is True

    def test_migrates_revealed_key_forwarded_correctly(
        self, tmp_path: Path
    ) -> None:
        """The revealed api_key is passed verbatim to _sync_to_native_provider."""
        fake_active = _make_provider(alias="Anthropic-test")
        fake_repo = MagicMock()
        fake_repo.get_active.return_value = fake_active
        fake_repo.reveal_api_key.return_value = "sk-anthropic-revealed"

        wiring = _make_wiring(provider_repo=fake_repo)

        with (
            patch(
                f"{_CONFIG_SOURCE_MODULE}._load_native_model_config",
                return_value=None,
            ),
            patch.object(wiring, "_sync_to_native_provider") as mock_sync,
        ):
            wiring.migrate_active_provider_to_native()

        mock_sync.assert_called_once_with(
            fake_active, "sk-anthropic-revealed", set_active=True
        )

    # -----------------------------------------------------------------------
    # 1b. Idempotence: native already populated → no sync
    # -----------------------------------------------------------------------

    def test_idempotent_when_native_already_has_active(
        self, tmp_path: Path
    ) -> None:
        """If native config already has an active provider, migration is a no-op."""
        fake_native_config = MagicMock()
        fake_repo = MagicMock()

        wiring = _make_wiring(provider_repo=fake_repo)

        with (
            patch(
                f"{_CONFIG_SOURCE_MODULE}._load_native_model_config",
                return_value=fake_native_config,
            ),
            patch.object(wiring, "_sync_to_native_provider") as mock_sync,
        ):
            wiring.migrate_active_provider_to_native()

        mock_sync.assert_not_called()
        fake_repo.get_active.assert_not_called()

    def test_idempotent_called_twice_syncs_only_once(
        self, tmp_path: Path
    ) -> None:
        """Second call sees native populated (after first sync) and skips."""
        fake_active = _make_provider()
        fake_repo = MagicMock()
        fake_repo.get_active.return_value = fake_active
        fake_repo.reveal_api_key.return_value = "sk-abc"

        wiring = _make_wiring(provider_repo=fake_repo)

        # First call: native is empty.
        # Second call: native is now populated (simulate by returning a config).
        native_results = [None, MagicMock()]

        with (
            patch(
                f"{_CONFIG_SOURCE_MODULE}._load_native_model_config",
                side_effect=native_results,
            ),
            patch.object(wiring, "_sync_to_native_provider") as mock_sync,
        ):
            wiring.migrate_active_provider_to_native()
            wiring.migrate_active_provider_to_native()

        assert mock_sync.call_count == 1

    # -----------------------------------------------------------------------
    # 1c. Fail-soft
    # -----------------------------------------------------------------------

    def test_migration_exception_does_not_propagate(self) -> None:
        """Any unhandled exception in the migration body must be swallowed."""
        fake_repo = MagicMock()
        fake_repo.get_active.side_effect = RuntimeError("DB exploded")

        wiring = _make_wiring(provider_repo=fake_repo)

        with patch(
            f"{_CONFIG_SOURCE_MODULE}._load_native_model_config",
            return_value=None,
        ):
            # Must not raise
            wiring.migrate_active_provider_to_native()

    def test_migration_reveal_failure_uses_none_key_and_still_syncs(
        self, tmp_path: Path
    ) -> None:
        """If reveal_api_key raises, sync proceeds with api_key=None (fail-soft)."""
        fake_active = _make_provider()
        fake_repo = MagicMock()
        fake_repo.get_active.return_value = fake_active
        fake_repo.reveal_api_key.side_effect = RuntimeError("vault unavailable")

        wiring = _make_wiring(provider_repo=fake_repo)

        with (
            patch(
                f"{_CONFIG_SOURCE_MODULE}._load_native_model_config",
                return_value=None,
            ),
            patch.object(wiring, "_sync_to_native_provider") as mock_sync,
        ):
            wiring.migrate_active_provider_to_native()

        mock_sync.assert_called_once_with(fake_active, None, set_active=True)

    def test_migration_no_op_when_no_sql_active(self) -> None:
        """When SQL has no active provider, _sync_to_native_provider is not called."""
        fake_repo = MagicMock()
        fake_repo.get_active.return_value = None

        wiring = _make_wiring(provider_repo=fake_repo)

        with (
            patch(
                f"{_CONFIG_SOURCE_MODULE}._load_native_model_config",
                return_value=None,
            ),
            patch.object(wiring, "_sync_to_native_provider") as mock_sync,
        ):
            wiring.migrate_active_provider_to_native()

        mock_sync.assert_not_called()

    def test_migration_no_op_when_provider_repo_is_none(self) -> None:
        """Wiring without provider_repo must not crash."""
        wiring = _make_wiring(provider_repo=None)

        with patch(
            f"{_CONFIG_SOURCE_MODULE}._load_native_model_config",
            return_value=None,
        ):
            wiring.migrate_active_provider_to_native()  # must not raise


# ---------------------------------------------------------------------------
# 2. Cascade — resolve_model_config no longer calls load_active_model_config
# ---------------------------------------------------------------------------

class TestResolveModelConfigCascade:

    def test_load_active_model_config_not_called_when_native_returns_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Native returns a config → SQL branch is never invoked."""
        from hermes.runtime.model_config import ModelConfig
        from hermes.runtime.provider_config_source import resolve_model_config

        fake_config = MagicMock(spec=ModelConfig)

        with (
            patch(
                f"{_CONFIG_SOURCE_MODULE}._load_native_model_config",
                return_value=fake_config,
            ),
            patch(
                f"{_CONFIG_SOURCE_MODULE}.load_active_model_config",
            ) as mock_sql,
        ):
            result = resolve_model_config(tmp_path / "nope.db")

        assert result is fake_config
        mock_sql.assert_not_called()

    def test_load_active_model_config_not_called_when_native_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Native returns None → cascade falls to env, NOT to SQL."""
        from hermes.runtime.provider_config_source import resolve_model_config

        monkeypatch.setenv("HERMES_MODEL", "anthropic/claude-3-5-haiku-20241022")

        with (
            patch(
                f"{_CONFIG_SOURCE_MODULE}._load_native_model_config",
                return_value=None,
            ),
            patch(
                f"{_CONFIG_SOURCE_MODULE}.load_active_model_config",
            ) as mock_sql,
        ):
            result = resolve_model_config(tmp_path / "nope.db")

        mock_sql.assert_not_called()
        assert result is not None
        assert result.model == "anthropic/claude-3-5-haiku-20241022"

    def test_resolve_returns_none_when_native_and_env_both_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No native + no env → None (not SQL)."""
        from hermes.runtime.provider_config_source import resolve_model_config

        monkeypatch.delenv("HERMES_MODEL", raising=False)

        with patch(
            f"{_CONFIG_SOURCE_MODULE}._load_native_model_config",
            return_value=None,
        ):
            result = resolve_model_config(tmp_path / "nope.db")

        assert result is None

    def test_db_path_argument_is_accepted_but_ignored(
        self, tmp_path: Path
    ) -> None:
        """resolve_model_config still accepts db_path for call-site compatibility."""
        from hermes.runtime.provider_config_source import resolve_model_config
        from hermes.runtime.model_config import ModelConfig

        fake_config = MagicMock(spec=ModelConfig)

        with patch(
            f"{_CONFIG_SOURCE_MODULE}._load_native_model_config",
            return_value=fake_config,
        ):
            result = resolve_model_config(tmp_path / "whatever.db")

        assert result is fake_config
