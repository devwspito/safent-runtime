"""VaultProviderResolver — the ONLY point that calls reveal_api_key().

Security invariant: the vault is decrypted in exactly one place (_build).
The decrypted key lives in ResolvedModel.api_key (repr-redacted, never logged,
never persisted). Every caller receives a ResolvedModel and passes the key
directly to the LLM engine as an explicit_api_key kwarg.

This resolver is fail-soft on individual errors so the daemon can start
degraded (no provider configured) rather than crashing.
"""

from __future__ import annotations

import logging

from hermes.providers.domain.canonical import CanonicalProvider
from hermes.providers.domain.catalog import canonical_for
from hermes.providers.domain.errors import ProviderResolutionError
from hermes.providers.domain.ports import ResolvedModel
from hermes.shell_server.providers.domain import Provider, ProviderKind
from hermes.shell_server.providers.repo import SQLiteProviderRepository

logger = logging.getLogger(__name__)


class VaultProviderResolver:
    """Resolves provider credentials from the SQLite vault.

    Single-decrypt-point invariant: reveal_api_key() is called ONLY in _build().
    All other methods delegate to _build() or return the already-constructed
    ResolvedModel.

    Thread safety: SQLiteProviderRepository uses its own connection per call;
    VaultProviderResolver itself is stateless between calls.
    """

    def __init__(self, repo: SQLiteProviderRepository) -> None:
        self._repo = repo

    # ------------------------------------------------------------------
    # ProviderResolver protocol implementation
    # ------------------------------------------------------------------

    def resolve_active(self) -> ResolvedModel | None:
        """Resolve the active provider from the vault. Fail-soft → None."""
        try:
            provider = self._repo.get_active()
        except Exception:
            logger.debug("hermes.providers.vault_resolver.get_active_failed", exc_info=True)
            return None

        if provider is None:
            return None

        return self._build(provider)

    def resolve_provider(self, kind: ProviderKind) -> ResolvedModel | None:
        """Resolve the first provider of a given kind. Fail-soft → None."""
        try:
            providers = self._repo.list_all()
        except Exception:
            logger.debug(
                "hermes.providers.vault_resolver.list_failed: kind=%s", kind, exc_info=True
            )
            return None

        for p in providers:
            if p.kind == kind:
                return self._build(p)

        return None

    def canonical_for(self, kind: ProviderKind) -> CanonicalProvider:
        """Return the canonical descriptor for a kind (never fails)."""
        return canonical_for(kind)

    # ------------------------------------------------------------------
    # Single decrypt point
    # ------------------------------------------------------------------

    def _build(self, provider: Provider) -> ResolvedModel | None:
        """Decrypt api_key and construct ResolvedModel.

        SECURITY: This is the ONLY method that calls reveal_api_key().
        The decrypted key is placed in ResolvedModel.api_key (repr-redacted).
        It must not be logged, stored, or passed anywhere except the engine adapter.
        """
        canonical = canonical_for(provider.kind)
        api_key: str | None = None

        if provider.has_api_key:
            try:
                api_key = self._repo.reveal_api_key(provider_id=provider.provider_id)
            except Exception as exc:
                logger.warning(
                    "hermes.providers.vault_resolver.decrypt_failed: "
                    "provider=%s kind=%s error=%s",
                    provider.alias,
                    provider.kind,
                    type(exc).__name__,
                )
                # Fail-soft: return ResolvedModel without key so the engine
                # can attempt resolution from env vars as fallback.

        effective_base_url = _resolve_base_url(provider, canonical)

        logger.info(
            "hermes.providers.vault_resolver.resolved: alias=%s kind=%s has_key=%s",
            provider.alias,
            provider.kind,
            api_key is not None,
        )

        return ResolvedModel(
            provider=provider,
            canonical=canonical,
            api_key=api_key,
            base_url=effective_base_url,
        )


def _resolve_base_url(provider: Provider, canonical: CanonicalProvider) -> str | None:
    """Determine effective base_url: provider-configured > catalog default > None."""
    if provider.base_url:
        return provider.base_url
    return canonical.default_base_url
