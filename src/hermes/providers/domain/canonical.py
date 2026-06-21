"""CanonicalProvider — per-kind static routing descriptor.

Holds everything the resolvers need to map a ProviderKind to the correct
call-site args for each target engine (litellm, hermes-agent Nous, NemoClaw).
Pure data, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class HermesCliRoute(Enum):
    """How to call resolve_runtime_provider() for a given ProviderKind.

    REGISTERED_SLUG:
        The provider has a first-class entry in hermes_cli PROVIDER_REGISTRY.
        Pass requested=hermes_cli_slug, no explicit_base_url needed.

    EXPLICIT_OPENAI_COMPAT:
        Route through the "openrouter" aggregator but with an explicit base_url
        that overrides the OpenRouter endpoint. hermes_cli picks the right api_key
        from explicit_api_key → env fallbacks. Used for: OpenAI, OpenRouter,
        Mistral, Cohere, Groq, Fireworks, Together.

    CUSTOM_BASE_URL:
        Provider requires a user-supplied base_url (self-hosted or Azure).
        Pass requested=hermes_cli_slug + explicit_base_url from provider.base_url.
    """

    REGISTERED_SLUG = "registered_slug"
    EXPLICIT_OPENAI_COMPAT = "explicit_openai_compat"
    CUSTOM_BASE_URL = "custom_base_url"


@dataclass(frozen=True, slots=True)
class CanonicalProvider:
    """Static routing descriptor for one ProviderKind.

    Attributes:
        litellm_prefix: Prefix string used to build the LiteLLM model string
            (e.g. "openai" → "openai/gpt-4o").
        hermes_cli_slug: Slug passed as `requested=` to resolve_runtime_provider().
        route: Routing strategy for hermes-agent (HermesCliRoute enum).
        requires_base_url: True if the provider MUST have a base_url configured.
        default_base_url: Canonical endpoint for EXPLICIT_OPENAI_COMPAT providers.
            None for providers where hermes_cli resolves the endpoint itself.
    """

    litellm_prefix: str
    hermes_cli_slug: str
    route: HermesCliRoute
    requires_base_url: bool = False
    default_base_url: str | None = None
