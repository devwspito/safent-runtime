"""Unit tests for the ProviderKind → hermes_cli native provider mapping.

Tests are intentionally hermetic: no hermes_cli, no SQLite, no filesystem.

Coverage:
  1. kind_to_native_target — full mapping table, known kinds, fallback.
  2. _sync_to_native_provider integration with mocked _write_hermes_env /
     _write_hermes_model_config — verifies that add_provider + set_active
     calls land the correct env-var write without touching hermes_cli.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from hermes.shell_server.providers.domain import ProviderKind
from hermes.shell_server.providers.native_sync import (
    NativeProviderTarget,
    kind_to_native_target,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# 1. Mapping table — kind_to_native_target
# ---------------------------------------------------------------------------


class TestKindToNativeTarget:
    def test_openai_maps_to_openai_api(self) -> None:
        t = kind_to_native_target(ProviderKind.OPENAI)
        assert t.provider_id == "openai-api"
        assert t.env_var == "OPENAI_API_KEY"
        assert t.base_url_env_var == "OPENAI_BASE_URL"
        assert t.needs_base_url is False

    def test_anthropic_maps_directly(self) -> None:
        t = kind_to_native_target(ProviderKind.ANTHROPIC)
        assert t.provider_id == "anthropic"
        assert t.env_var == "ANTHROPIC_API_KEY"
        assert t.needs_base_url is False

    def test_gemini_maps_directly(self) -> None:
        t = kind_to_native_target(ProviderKind.GEMINI)
        assert t.provider_id == "gemini"
        assert t.env_var == "GOOGLE_API_KEY"
        assert t.needs_base_url is False

    def test_deepseek_maps_directly(self) -> None:
        t = kind_to_native_target(ProviderKind.DEEPSEEK)
        assert t.provider_id == "deepseek"
        assert t.env_var == "DEEPSEEK_API_KEY"
        assert t.needs_base_url is False

    def test_lm_studio_maps_to_lmstudio(self) -> None:
        t = kind_to_native_target(ProviderKind.LM_STUDIO)
        assert t.provider_id == "lmstudio"
        assert t.env_var == "LM_API_KEY"
        assert t.needs_base_url is True

    def test_ollama_maps_to_ollama_cloud(self) -> None:
        t = kind_to_native_target(ProviderKind.OLLAMA)
        assert t.provider_id == "ollama-cloud"
        assert t.needs_base_url is True

    def test_vllm_maps_to_openai_api_with_base_url(self) -> None:
        t = kind_to_native_target(ProviderKind.VLLM)
        assert t.provider_id == "openai-api"
        assert t.needs_base_url is True

    def test_openai_compatible_falls_back_to_openai_api(self) -> None:
        t = kind_to_native_target(ProviderKind.OPENAI_COMPATIBLE)
        assert t.provider_id == "openai-api"
        assert t.needs_base_url is True

    def test_groq_maps_to_openai_api_needs_base_url(self) -> None:
        # groq is not in PROVIDER_REGISTRY — mapped via openai-api
        t = kind_to_native_target(ProviderKind.GROQ)
        assert t.provider_id == "openai-api"
        assert t.needs_base_url is True

    def test_mistral_maps_to_openai_api_needs_base_url(self) -> None:
        t = kind_to_native_target(ProviderKind.MISTRAL)
        assert t.provider_id == "openai-api"
        assert t.needs_base_url is True

    def test_openrouter_maps_to_openai_api(self) -> None:
        t = kind_to_native_target(ProviderKind.OPENROUTER)
        assert t.provider_id == "openai-api"
        assert t.needs_base_url is True

    def test_nous_has_empty_env_var(self) -> None:
        # NOUS is OAuth — no api_key path; env_var is intentionally empty.
        t = kind_to_native_target(ProviderKind.NOUS)
        assert t.provider_id == "nous"
        assert t.env_var == ""

    def test_qwen_dashscope_maps_to_alibaba(self) -> None:
        t = kind_to_native_target(ProviderKind.QWEN_DASHSCOPE)
        assert t.provider_id == "alibaba"

    def test_moonshot_maps_to_kimi_coding(self) -> None:
        t = kind_to_native_target(ProviderKind.MOONSHOT)
        assert t.provider_id == "kimi-coding"

    def test_all_provider_kinds_are_covered(self) -> None:
        """Every ProviderKind must produce a valid NativeProviderTarget (no crash)."""
        for kind in ProviderKind:
            target = kind_to_native_target(kind)
            assert isinstance(target, NativeProviderTarget), f"kind {kind!r} returned bad type"
            assert target.provider_id, f"kind {kind!r} has empty provider_id"

    def test_hypothetical_unknown_kind_falls_back_gracefully(self) -> None:
        """Simulates a future ProviderKind added without updating the map.

        We cannot create an actual unknown ProviderKind (StrEnum rejects it), but
        we can assert the table fallback logic directly via a mock kind value.
        """
        from hermes.shell_server.providers import native_sync as _ns

        # Temporarily clear the map entry for VLLM to exercise the fallback path.
        original = _ns._KIND_MAP.get(ProviderKind.VLLM)
        try:
            del _ns._KIND_MAP[ProviderKind.VLLM]
            t = kind_to_native_target(ProviderKind.VLLM)
            assert t.provider_id == "openai-api"
            assert t.needs_base_url is True
        finally:
            if original is not None:
                _ns._KIND_MAP[ProviderKind.VLLM] = original


# ---------------------------------------------------------------------------
# 2. _sync_to_native_provider wiring — mocked writes, no hermes_cli needed
# ---------------------------------------------------------------------------

def _make_provider(
    *,
    kind: ProviderKind = ProviderKind.OPENAI,
    base_url: str | None = None,
    default_model: str = "gpt-5.4-nano",
) -> Any:
    """Build a minimal Provider-like object for wiring tests."""
    from hermes.shell_server.providers.domain import new_provider

    return new_provider(
        alias="test-provider",
        kind=kind,
        default_model=default_model,
        base_url=base_url,
        has_api_key=True,
    )


_DBUS_MODULE = "hermes.agents_os.infrastructure.dbus_runtime_service"
_NATIVE_SYNC_MODULE = "hermes.shell_server.providers.native_sync"


class TestSyncToNativeProvider:
    """Tests for DbusRuntimeServiceWiring._sync_to_native_provider."""

    def _make_wiring(self, tmp_path: Path) -> Any:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            DbusRuntimeServiceWiring,
        )
        from hermes.shell_server.providers.repo import SQLiteProviderRepository
        from hermes.shell_server.security.secrets import SecretsVault
        from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

        class _NullGate:
            async def register_pending(self, *, proposal_id, **_) -> None: ...
            async def approve(self, *, proposal_id, approved_by) -> str:
                return ""
            async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
            async def verify_token(self, *, proposal_id, token) -> bool:
                return False
            async def approved_token_for(self, proposal_id) -> str | None:
                return None

        vault = SecretsVault(master_key=os.urandom(32))
        repo = SQLiteProviderRepository(db_path=tmp_path / "p.db", vault=vault)
        return DbusRuntimeServiceWiring(
            agent_state=InMemoryAgentState(),
            approval_gate=_NullGate(),
            authorized_uids=frozenset({1000}),
            provider_repo=repo,
        )

    def _draft(self, **kwargs: Any) -> str:
        base = {
            "kind": "openai",
            "alias": "OpenAI-test",
            "default_model": "gpt-5.4-nano",
            "api_key": "sk-test-123",
            "set_active": False,
        }
        base.update(kwargs)
        return json.dumps(base)

    def test_add_provider_calls_write_env_when_api_key_present(
        self, tmp_path: Path
    ) -> None:
        """add_provider with an api_key must call _write_hermes_env fail-soft."""
        wiring = self._make_wiring(tmp_path)

        with (
            patch(f"{_DBUS_MODULE}._write_hermes_env") as mock_write_env,
            patch(f"{_DBUS_MODULE}._write_hermes_model_config") as mock_write_model,
            patch(f"{_NATIVE_SYNC_MODULE}.kind_to_native_target") as mock_map,
        ):
            from hermes.shell_server.providers.native_sync import NativeProviderTarget

            mock_map.return_value = NativeProviderTarget(
                provider_id="openai-api",
                env_var="OPENAI_API_KEY",
                base_url_env_var="OPENAI_BASE_URL",
                needs_base_url=False,
            )
            # Fake PROVIDER_REGISTRY so the env_var validation branch passes.
            fake_registry = {
                "openai-api": MagicMock(
                    api_key_env_vars=("OPENAI_API_KEY",),
                    base_url_env_var="OPENAI_BASE_URL",
                )
            }
            with patch.dict(
                "sys.modules",
                {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=fake_registry)},
            ):
                wiring.add_provider(
                    draft_json=self._draft(set_active=False),
                    sender_uid=1000,
                )

        mock_write_env.assert_called_once_with("OPENAI_API_KEY", "sk-test-123")
        mock_write_model.assert_not_called()  # set_active=False → no model config write

    def test_add_provider_with_set_active_writes_model_config(
        self, tmp_path: Path
    ) -> None:
        """set_active=True must trigger _write_hermes_model_config."""
        wiring = self._make_wiring(tmp_path)

        with (
            patch(f"{_DBUS_MODULE}._write_hermes_env"),
            patch(f"{_DBUS_MODULE}._write_hermes_model_config") as mock_write_model,
            patch(f"{_NATIVE_SYNC_MODULE}.kind_to_native_target") as mock_map,
        ):
            from hermes.shell_server.providers.native_sync import NativeProviderTarget

            mock_map.return_value = NativeProviderTarget(
                provider_id="openai-api",
                env_var="OPENAI_API_KEY",
                base_url_env_var="OPENAI_BASE_URL",
                needs_base_url=False,
            )
            fake_registry = {
                "openai-api": MagicMock(
                    api_key_env_vars=("OPENAI_API_KEY",),
                    base_url_env_var="OPENAI_BASE_URL",
                )
            }
            with patch.dict(
                "sys.modules",
                {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=fake_registry)},
            ):
                wiring.add_provider(
                    draft_json=self._draft(set_active=True),
                    sender_uid=1000,
                )

        mock_write_model.assert_called_once_with("openai-api", "gpt-5.4-nano", "")

    def test_set_active_provider_writes_model_config_and_env(
        self, tmp_path: Path
    ) -> None:
        """set_active_provider must call _write_hermes_model_config for the new active."""
        wiring = self._make_wiring(tmp_path)
        saved = wiring.add_provider(draft_json=self._draft(), sender_uid=1000)
        pid = saved["provider_id"]

        with (
            patch(f"{_DBUS_MODULE}._write_hermes_env") as mock_write_env,
            patch(f"{_DBUS_MODULE}._write_hermes_model_config") as mock_write_model,
            patch(f"{_NATIVE_SYNC_MODULE}.kind_to_native_target") as mock_map,
        ):
            from hermes.shell_server.providers.native_sync import NativeProviderTarget

            mock_map.return_value = NativeProviderTarget(
                provider_id="openai-api",
                env_var="OPENAI_API_KEY",
                base_url_env_var="OPENAI_BASE_URL",
                needs_base_url=False,
            )
            fake_registry = {
                "openai-api": MagicMock(
                    api_key_env_vars=("OPENAI_API_KEY",),
                    base_url_env_var="OPENAI_BASE_URL",
                )
            }
            with patch.dict(
                "sys.modules",
                {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=fake_registry)},
            ):
                wiring.set_active_provider(provider_id=pid, sender_uid=1000)

        mock_write_env.assert_called_once_with("OPENAI_API_KEY", "sk-test-123")
        mock_write_model.assert_called_once_with("openai-api", "gpt-5.4-nano", "")

    def test_native_sync_skipped_for_nous_oauth_kind(self, tmp_path: Path) -> None:
        """NOUS (OAuth, no api_key) must not invoke _write_hermes_env."""
        wiring = self._make_wiring(tmp_path)

        with (
            patch(f"{_DBUS_MODULE}._write_hermes_env") as mock_write_env,
            patch(f"{_DBUS_MODULE}._write_hermes_model_config"),
            patch(f"{_NATIVE_SYNC_MODULE}.kind_to_native_target") as mock_map,
        ):
            from hermes.shell_server.providers.native_sync import NativeProviderTarget

            # NOUS target has empty env_var — the sync should short-circuit.
            mock_map.return_value = NativeProviderTarget(
                provider_id="nous",
                env_var="",
                base_url_env_var="",
                needs_base_url=False,
            )
            fake_registry = {"nous": MagicMock(api_key_env_vars=(), base_url_env_var="")}
            with patch.dict(
                "sys.modules",
                {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=fake_registry)},
            ):
                wiring.add_provider(
                    draft_json=self._draft(kind="nous"),
                    sender_uid=1000,
                )

        mock_write_env.assert_not_called()

    def test_native_sync_skipped_when_no_api_key(self, tmp_path: Path) -> None:
        """Provider without api_key must not write to hermes_cli (no key to forward)."""
        wiring = self._make_wiring(tmp_path)

        with (
            patch(f"{_DBUS_MODULE}._write_hermes_env") as mock_write_env,
            patch(f"{_DBUS_MODULE}._write_hermes_model_config"),
            patch(f"{_NATIVE_SYNC_MODULE}.kind_to_native_target") as mock_map,
        ):
            from hermes.shell_server.providers.native_sync import NativeProviderTarget

            mock_map.return_value = NativeProviderTarget(
                provider_id="openai-api",
                env_var="OPENAI_API_KEY",
                base_url_env_var="OPENAI_BASE_URL",
                needs_base_url=False,
            )
            fake_registry = {
                "openai-api": MagicMock(
                    api_key_env_vars=("OPENAI_API_KEY",),
                    base_url_env_var="OPENAI_BASE_URL",
                )
            }
            with patch.dict(
                "sys.modules",
                {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=fake_registry)},
            ):
                # No api_key in draft
                draft = json.dumps({
                    "kind": "openai",
                    "alias": "OpenAI-no-key",
                    "default_model": "gpt-5.4-nano",
                })
                wiring.add_provider(draft_json=draft, sender_uid=1000)

        mock_write_env.assert_not_called()

    def test_native_sync_failure_does_not_break_sql_write(
        self, tmp_path: Path
    ) -> None:
        """A crash in _sync_to_native_provider must not propagate: SQL store prevails."""
        wiring = self._make_wiring(tmp_path)

        with (
            patch(
                f"{_NATIVE_SYNC_MODULE}.kind_to_native_target",
                side_effect=RuntimeError("simulated native failure"),
            ),
            patch.dict(
                "sys.modules",
                {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY={})},
            ),
        ):
            result = wiring.add_provider(
                draft_json=self._draft(), sender_uid=1000
            )

        # The SQL write must have succeeded despite the native sync crash.
        assert result["alias"] == "OpenAI-test"
        assert result["has_api_key"] is True
        listed = wiring.list_providers()
        assert len(listed) == 1

    def test_native_sync_skipped_when_hermes_cli_absent(
        self, tmp_path: Path
    ) -> None:
        """If hermes_cli is not installed, _sync_to_native silently returns."""
        wiring = self._make_wiring(tmp_path)

        with patch(
            f"{_NATIVE_SYNC_MODULE}.kind_to_native_target",
            side_effect=ImportError("hermes_cli not found"),
        ):
            result = wiring.add_provider(
                draft_json=self._draft(), sender_uid=1000
            )

        assert result["alias"] == "OpenAI-test"
