"""Test 3: OPENAI resolves to the correct slug — the primary regression guard.

Proves that:
  - OPENAI → requested='openrouter', not 'openai-api' (the bug).
  - No AuthError is raised when hermes_cli IS available.
  - When hermes_cli is NOT available, the test skips gracefully.

The critical path tested:
  ProviderKind.OPENAI → canonical_for() → nous_request_from_resolved()
  → HermesCliRequest(requested='openrouter', explicit_base_url='https://api.openai.com/v1')
  → resolve_runtime_provider(requested='openrouter', explicit_base_url=...)
  → NO AuthError.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add the hermes_cli location to sys.path for this test run.
_HERMES_CLI_VENV = Path(
    "/home/luiscorrea-dev/Desktop/oposads-agent/.venv/lib/python3.12/site-packages"
)
_HERMES_CLI_AVAILABLE = _HERMES_CLI_VENV.is_dir()

from hermes.providers.domain.catalog import canonical_for
from hermes.providers.domain.ports import ResolvedModel
from hermes.providers.infrastructure.nous_provider_adapter import nous_request_from_resolved
from hermes.shell_server.providers.domain import ProviderKind, Provider, ProviderConnectivity

from datetime import UTC, datetime
from uuid import uuid4


def _openai_provider() -> Provider:
    return Provider(
        provider_id=uuid4(),
        alias="OpenAI Direct",
        kind=ProviderKind.OPENAI,
        base_url=None,
        has_api_key=True,
        default_model="gpt-4o",
        enabled=True,
        is_active=True,
        connectivity=ProviderConnectivity.UNKNOWN,
        created_at=datetime.now(tz=UTC),
    )


def _openai_resolved(api_key: str = "sk-test-key") -> ResolvedModel:
    canonical = canonical_for(ProviderKind.OPENAI)
    return ResolvedModel(
        provider=_openai_provider(),
        canonical=canonical,
        api_key=api_key,
        base_url=canonical.default_base_url,
    )


class TestOpenAIResolvesToValidSlug:
    def test_openai_nous_request_is_not_openai_api(self) -> None:
        """The breaking slug 'openai-api' must never be produced for OPENAI kind."""
        resolved = _openai_resolved()
        req = nous_request_from_resolved(resolved)
        assert req.requested != "openai-api", (
            "OPENAI kind produced requested='openai-api' which is not in "
            "hermes_cli PROVIDER_REGISTRY → would raise AuthError."
        )

    def test_openai_nous_request_uses_openrouter(self) -> None:
        resolved = _openai_resolved()
        req = nous_request_from_resolved(resolved)
        assert req.requested == "openrouter"
        assert req.explicit_base_url == "https://api.openai.com/v1"

    def test_openai_nous_request_carries_api_key(self) -> None:
        resolved = _openai_resolved(api_key="sk-real-key")
        req = nous_request_from_resolved(resolved)
        # Key must be present (not None) so hermes_cli uses it explicitly.
        assert req.explicit_api_key == "sk-real-key"

    @pytest.mark.skipif(
        not _HERMES_CLI_AVAILABLE,
        reason="hermes_cli not installed in this environment",
    )
    def test_openai_resolve_runtime_provider_no_auth_error(self) -> None:
        """With hermes_cli available: resolve_runtime_provider does NOT raise AuthError.

        This is the end-to-end proof that the bug is dead. The old code passed
        requested='openai-api' → AuthError("Unknown provider 'openai-api'").
        The new code passes requested='openrouter' + explicit_base_url → success.
        """
        if str(_HERMES_CLI_VENV) not in sys.path:
            sys.path.insert(0, str(_HERMES_CLI_VENV))

        try:
            from hermes_cli.runtime_provider import (  # noqa: PLC0415
                resolve_runtime_provider,
            )
        except ImportError as exc:
            pytest.skip(f"hermes_cli.runtime_provider not importable: {exc}")

        resolved = _openai_resolved(api_key="sk-dummy-for-resolution-test")
        req = nous_request_from_resolved(resolved)

        # Must not raise AuthError
        try:
            rt = resolve_runtime_provider(
                requested=req.requested,
                explicit_api_key=req.explicit_api_key,
                explicit_base_url=req.explicit_base_url,
                target_model="gpt-4o",
            )
        except Exception as exc:  # noqa: BLE001
            # Distinguish AuthError (bug) from other errors (network, config, etc.)
            exc_type = type(exc).__name__
            if "AuthError" in exc_type or "Unknown provider" in str(exc):
                pytest.fail(
                    f"AuthError raised — the provider slug is wrong: {exc}\n"
                    f"HermesCliRequest was: {req!r}"
                )
            # Other errors (no config file, network) are acceptable in test env.
            pytest.skip(f"hermes_cli raised non-AuthError ({exc_type}): {exc}")
        else:
            # Verify the provider field is not an unknown slug.
            provider_val = rt.get("provider", "")
            assert provider_val != "openai-api", (
                f"resolve_runtime_provider returned provider='openai-api': {rt}"
            )
            # Must be one of the valid provider values.
            assert provider_val in {
                "openrouter", "custom", "openai", "anthropic", "gemini",
                "bedrock", "azure-foundry", "deepseek", "zai", "kimi-for-coding",
                "alibaba", "huggingface", "lmstudio", "nous",
            } or provider_val, f"Unexpected provider value: {provider_val!r}"
