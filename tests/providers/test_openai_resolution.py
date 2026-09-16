"""Test 3: OPENAI resolves to the correct slug — the primary regression guard.

Proves that:
  - OPENAI → requested='openrouter', not 'openai-api' (the bug).
  - No AuthError is raised for the fixed slug (via a fake hermes_cli.runtime_provider
    — see fake_hermes_cli_runtime_provider — since hermes_cli only ships inside the
    hermes-agent tarball, not on PyPI or this host: ops/hermes-agent.lock).
  - The old slug ('openai-api') DOES raise AuthError against the same fake.

The critical path tested:
  ProviderKind.OPENAI → canonical_for() → nous_request_from_resolved()
  → HermesCliRequest(requested='openrouter', explicit_base_url='https://api.openai.com/v1')
  → resolve_runtime_provider(requested='openrouter', explicit_base_url=...)
  → NO AuthError.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from hermes.providers.domain.catalog import canonical_for
from hermes.providers.domain.ports import ResolvedModel
from hermes.providers.infrastructure.nous_provider_adapter import nous_request_from_resolved
from hermes.shell_server.providers.domain import Provider, ProviderConnectivity, ProviderKind


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


_KNOWN_PROVIDER_REGISTRY_SLUGS = {
    "openrouter", "custom", "openai", "anthropic", "gemini",
    "bedrock", "azure-foundry", "deepseek", "zai", "kimi-for-coding",
    "alibaba", "huggingface", "lmstudio", "nous",
}


@pytest.fixture()
def fake_hermes_cli_runtime_provider(monkeypatch: pytest.MonkeyPatch):
    """Fake stand-in for `hermes_cli.runtime_provider.resolve_runtime_provider`.

    hermes_cli ships only inside the pinned hermes-agent GitHub tarball (see
    ops/hermes-agent.lock) — it is not on PyPI and not installed on this host
    or in CI. A prior version of this test reached into another project's
    venv on the machine (`sys.path.insert(0, "/home/.../oposads-agent/.venv/
    .../site-packages")`) to get a real one, and never cleaned up: that venv
    also ships a top-level `tools/` package, which shadowed hermes-agent's
    own `tools.memory_tool`/`tools.clarify_tool` for the rest of the pytest
    session and broke tests/security/test_broker_gate_hardening_iter3.py.

    This fake models just enough of the real contract (AuthError for a slug
    PROVIDER_REGISTRY doesn't know) to pin the regression, is injected via
    `monkeypatch.setitem(sys.modules, ...)` so it is undone automatically at
    the end of THIS test only, and needs no real install anywhere.
    """

    class AuthError(Exception):
        pass

    def resolve_runtime_provider(
        *,
        requested: str,
        explicit_api_key: str | None,
        explicit_base_url: str | None,
        target_model: str,
    ) -> dict:
        if requested not in _KNOWN_PROVIDER_REGISTRY_SLUGS:
            raise AuthError(f"Unknown provider {requested!r}")
        return {
            "provider": requested,
            "base_url": explicit_base_url,
            "api_key": explicit_api_key,
            "model": target_model,
        }

    runtime_provider_module = types.ModuleType("hermes_cli.runtime_provider")
    runtime_provider_module.resolve_runtime_provider = resolve_runtime_provider
    runtime_provider_module.AuthError = AuthError

    hermes_cli_module = types.ModuleType("hermes_cli")
    hermes_cli_module.runtime_provider = runtime_provider_module

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", runtime_provider_module)


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

    @pytest.mark.usefixtures("fake_hermes_cli_runtime_provider")
    def test_openai_resolve_runtime_provider_no_auth_error(self) -> None:
        """resolve_runtime_provider does NOT raise AuthError for the fixed slug.

        This is the end-to-end proof that the bug is dead. The old code passed
        requested='openai-api' → AuthError("Unknown provider 'openai-api'").
        The new code passes requested='openrouter' + explicit_base_url → success.
        """
        from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: PLC0415

        resolved = _openai_resolved(api_key="sk-dummy-for-resolution-test")
        req = nous_request_from_resolved(resolved)

        rt = resolve_runtime_provider(
            requested=req.requested,
            explicit_api_key=req.explicit_api_key,
            explicit_base_url=req.explicit_base_url,
            target_model="gpt-4o",
        )

        assert rt["provider"] != "openai-api", (
            f"resolve_runtime_provider returned provider='openai-api': {rt}"
        )
        assert rt["provider"] == "openrouter"

    @pytest.mark.usefixtures("fake_hermes_cli_runtime_provider")
    def test_openai_resolve_runtime_provider_raises_auth_error_for_old_bug(self) -> None:
        """Pins WHY the fix matters: the old slug is not in PROVIDER_REGISTRY."""
        from hermes_cli.runtime_provider import AuthError, resolve_runtime_provider  # noqa: PLC0415

        with pytest.raises(AuthError):
            resolve_runtime_provider(
                requested="openai-api",
                explicit_api_key="sk-test",
                explicit_base_url="https://api.openai.com/v1",
                target_model="gpt-4o",
            )
