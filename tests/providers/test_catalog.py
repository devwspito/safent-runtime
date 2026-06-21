"""Tests 1 & 2: catalog completeness + resolution matrix.

test_canonical_catalog_completeness — every ProviderKind has a catalog entry
  with valid fields. This would have caught the 'openai-api' bug at definition
  time: OPENAI now maps to hermes_cli_slug='openrouter', not 'openai-api'.

test_resolution_matrix — (kind × engine) produce coherent derivations:
  - litellm: correct prefix/model string.
  - nous: HermesCliRequest fields consistent with the route.
  - nemoclaw: route string is non-empty.

These run WITHOUT hermes_cli installed (pure domain logic only).
"""

from __future__ import annotations

import pytest

from hermes.providers.domain.canonical import HermesCliRoute
from hermes.providers.domain.catalog import (
    _CATALOG,
    build_litellm_model_string,
    canonical_for,
)
from hermes.providers.infrastructure.nemoclaw_provider_adapter import (
    nemoclaw_route_from_resolved,
)
from hermes.providers.infrastructure.nous_provider_adapter import (
    nous_request_from_resolved,
)
from hermes.shell_server.providers.domain import ProviderKind

# ── Fixtures ────────────────────────────────────────────────────────────────

from pathlib import Path
from uuid import uuid4
from datetime import UTC, datetime

from hermes.shell_server.providers.domain import Provider, ProviderConnectivity


def _make_provider(kind: ProviderKind, base_url: str | None = None) -> Provider:
    return Provider(
        provider_id=uuid4(),
        alias=f"test-{kind.value}",
        kind=kind,
        base_url=base_url,
        has_api_key=True,
        default_model="test-model",
        enabled=True,
        is_active=False,
        connectivity=ProviderConnectivity.UNKNOWN,
        created_at=datetime.now(tz=UTC),
    )


def _make_resolved(kind: ProviderKind, base_url: str | None = None):
    """Build a ResolvedModel for a given kind without touching the vault."""
    from hermes.providers.domain.ports import ResolvedModel

    canonical = canonical_for(kind)
    provider = _make_provider(kind, base_url=base_url or canonical.default_base_url)
    return ResolvedModel(
        provider=provider,
        canonical=canonical,
        api_key="sk-test-key",
        base_url=base_url or canonical.default_base_url,
    )


# ── Test 1: Catalog completeness ─────────────────────────────────────────────

class TestCatalogCompleteness:
    def test_all_provider_kinds_have_entry(self) -> None:
        """Every ProviderKind must have an entry in _CATALOG."""
        missing = [k for k in ProviderKind if k not in _CATALOG]
        assert missing == [], f"ProviderKinds missing from catalog: {missing}"

    def test_no_slug_is_openai_api(self) -> None:
        """The old broken slug 'openai-api' must not appear anywhere in the catalog.

        This is the root-cause regression guard: 'openai-api' is not in
        PROVIDER_REGISTRY of hermes_cli and raises AuthError every time.
        """
        bad_slugs = [
            (kind, entry.hermes_cli_slug)
            for kind, entry in _CATALOG.items()
            if entry.hermes_cli_slug == "openai-api"
        ]
        assert bad_slugs == [], (
            f"Catalog contains the broken slug 'openai-api' for: {bad_slugs}. "
            "This slug does not exist in hermes_cli PROVIDER_REGISTRY."
        )

    def test_openai_maps_to_openrouter_with_base_url(self) -> None:
        """OPENAI kind must use the EXPLICIT_OPENAI_COMPAT route with openai base_url."""
        entry = _CATALOG[ProviderKind.OPENAI]
        assert entry.route is HermesCliRoute.EXPLICIT_OPENAI_COMPAT
        assert entry.hermes_cli_slug == "openrouter"
        assert entry.default_base_url == "https://api.openai.com/v1"

    @pytest.mark.parametrize("kind", list(ProviderKind))
    def test_litellm_prefix_is_nonempty(self, kind: ProviderKind) -> None:
        entry = canonical_for(kind)
        assert entry.litellm_prefix, f"{kind}: litellm_prefix is empty"

    @pytest.mark.parametrize("kind", list(ProviderKind))
    def test_hermes_cli_slug_is_nonempty(self, kind: ProviderKind) -> None:
        entry = canonical_for(kind)
        assert entry.hermes_cli_slug, f"{kind}: hermes_cli_slug is empty"

    @pytest.mark.parametrize("kind", list(ProviderKind))
    def test_route_is_valid_enum(self, kind: ProviderKind) -> None:
        entry = canonical_for(kind)
        assert isinstance(entry.route, HermesCliRoute)

    def test_requires_base_url_kinds_have_custom_or_foundry_slug(self) -> None:
        """Providers that require_base_url should use 'custom' or a CUSTOM_BASE_URL route."""
        for kind, entry in _CATALOG.items():
            if entry.requires_base_url:
                assert entry.route in {
                    HermesCliRoute.CUSTOM_BASE_URL,
                    HermesCliRoute.REGISTERED_SLUG,  # lmstudio has its own slug
                }, f"{kind}: requires_base_url but route is {entry.route}"


# ── Test 2: Resolution matrix ─────────────────────────────────────────────────

class TestResolutionMatrix:
    @pytest.mark.parametrize("kind", list(ProviderKind))
    def test_litellm_model_string_has_prefix(self, kind: ProviderKind) -> None:
        entry = canonical_for(kind)
        result = build_litellm_model_string(kind, "some-model")
        assert result.startswith(entry.litellm_prefix + "/"), (
            f"{kind}: expected prefix '{entry.litellm_prefix}/' in '{result}'"
        )

    def test_litellm_model_string_idempotent(self) -> None:
        """If model already has the prefix, it must not be doubled."""
        result = build_litellm_model_string(ProviderKind.OPENAI, "openai/gpt-4o")
        assert result == "openai/gpt-4o"

    @pytest.mark.parametrize("kind", list(ProviderKind))
    def test_nous_request_requested_is_nonempty(self, kind: ProviderKind) -> None:
        canonical = canonical_for(kind)
        base_url = "http://localhost:8000" if canonical.requires_base_url else None
        resolved = _make_resolved(kind, base_url=base_url)
        req = nous_request_from_resolved(resolved)
        assert req.requested, f"{kind}: nous request.requested is empty"

    def test_nous_request_openai_uses_openai_base_url(self) -> None:
        """OPENAI → requested='openrouter', explicit_base_url='https://api.openai.com/v1'."""
        resolved = _make_resolved(ProviderKind.OPENAI)
        req = nous_request_from_resolved(resolved)
        assert req.requested == "openrouter"
        assert req.explicit_base_url == "https://api.openai.com/v1"

    def test_nous_request_anthropic_uses_registered_slug(self) -> None:
        resolved = _make_resolved(ProviderKind.ANTHROPIC)
        req = nous_request_from_resolved(resolved)
        assert req.requested == "anthropic"

    def test_nous_request_key_not_in_repr(self) -> None:
        resolved = _make_resolved(ProviderKind.OPENAI)
        req = nous_request_from_resolved(resolved)
        assert "sk-test-key" not in repr(req)

    @pytest.mark.parametrize("kind", list(ProviderKind))
    def test_nemoclaw_route_is_nonempty(self, kind: ProviderKind) -> None:
        canonical = canonical_for(kind)
        base_url = "http://localhost:8000" if canonical.requires_base_url else None
        resolved = _make_resolved(kind, base_url=base_url)
        route = nemoclaw_route_from_resolved(resolved)
        assert route, f"{kind}: nemoclaw route is empty"
