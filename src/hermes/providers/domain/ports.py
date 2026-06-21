"""Domain ports for provider resolution.

Protocol definitions (structural typing) for the resolver + value objects
for resolved credentials. Pure domain — no I/O, no framework deps.

Security invariants:
  - ResolvedModel.api_key is transitoria: never persisted, never serialized,
    never logged. __repr__ is redacted to prevent accidental leakage.
  - HermesCliRequest carries the api_key only to pass to resolve_runtime_provider();
    it must not outlive the call-site scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from hermes.providers.domain.canonical import CanonicalProvider
from hermes.shell_server.providers.domain import Provider, ProviderKind


@dataclass(frozen=True)
class ResolvedModel:
    """Fully resolved provider credentials + routing for one engine call.

    api_key is transitoria — it is decrypted from the vault exclusively by
    VaultProviderResolver._build() and must be consumed immediately by the
    adapter that calls the LLM.  It is never written to disk, env, or logs.

    base_url is None when the provider manages its own canonical endpoint
    (e.g. Anthropic, Gemini). It is required for self-hosted and Azure.
    """

    provider: Provider
    canonical: CanonicalProvider
    api_key: str | None = field(repr=False)  # never repr'd — leak prevention
    base_url: str | None = None

    def __repr__(self) -> str:  # noqa: D105
        has_key = self.api_key is not None
        return (
            f"ResolvedModel(provider={self.provider.alias!r}, "
            f"kind={self.provider.kind!r}, "
            f"api_key=<{'set' if has_key else 'unset'}>, "
            f"base_url={self.base_url!r})"
        )


@dataclass(frozen=True)
class HermesCliRequest:
    """Arguments to pass to resolve_runtime_provider() for Nous / hermes-agent.

    Produced by NousProviderAdapter from a ResolvedModel.  api_key is tagged
    with repr=False — same leak-prevention as ResolvedModel.api_key.
    """

    requested: str
    explicit_api_key: str | None = field(repr=False)
    explicit_base_url: str | None = None
    target_model: str | None = None

    def __repr__(self) -> str:  # noqa: D105
        has_key = self.explicit_api_key is not None
        return (
            f"HermesCliRequest(requested={self.requested!r}, "
            f"explicit_api_key=<{'set' if has_key else 'unset'}>, "
            f"explicit_base_url={self.explicit_base_url!r})"
        )


@runtime_checkable
class ProviderResolver(Protocol):
    """Port: resolves the active provider to a ResolvedModel.

    Implementations live in infrastructure (VaultProviderResolver, etc).
    Application layer depends only on this protocol.
    """

    def resolve_active(self) -> ResolvedModel | None:
        """Return the ResolvedModel for the active provider, or None.

        None means no provider is configured (CI/headless/no onboarding).
        Must not raise — fail-soft so the caller can fall back to env.
        """
        ...

    def resolve_provider(self, kind: ProviderKind) -> ResolvedModel | None:
        """Return the ResolvedModel for the first provider of a given kind, or None."""
        ...

    def canonical_for(self, kind: ProviderKind) -> CanonicalProvider:
        """Return the CanonicalProvider for a ProviderKind (never fails)."""
        ...
