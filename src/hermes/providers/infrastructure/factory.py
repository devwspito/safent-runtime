"""Factory for building a ProviderResolver from a database path.

Call build_provider_resolver() once at daemon startup and inject the result
into all components that need provider resolution. This is the composition
root for the providers bounded context.
"""

from __future__ import annotations

import logging
from pathlib import Path

from hermes.providers.infrastructure.vault_provider_resolver import VaultProviderResolver

logger = logging.getLogger(__name__)


def build_provider_resolver(db_path: Path) -> VaultProviderResolver | None:
    """Build a VaultProviderResolver for the given database path.

    Returns None (fail-soft) if the vault cannot be initialised (e.g. master.key
    absent in CI, DB not yet created). Callers must handle None gracefully.

    Security: SecretsVault() reads master.key at construction time. If the file
    is absent or corrupt, SecretsVault raises RuntimeError (fail-closed design).
    We catch and demote to a warning so the daemon starts degraded.
    """
    try:
        from hermes.shell_server.providers.repo import (  # noqa: PLC0415
            SQLiteProviderRepository,
        )
        from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415
    except ImportError:
        logger.debug("hermes.providers.factory.shell_server_unavailable")
        return None

    try:
        vault = SecretsVault()
        repo = SQLiteProviderRepository(db_path=db_path, vault=vault)
        return VaultProviderResolver(repo=repo)
    except RuntimeError as exc:
        # master.key absent / corrupt — expected in CI and first-boot before keygen.
        logger.warning(
            "hermes.providers.factory.vault_unavailable: %s — resolver not built", exc
        )
        return None
    except Exception as exc:
        logger.warning(
            "hermes.providers.factory.build_failed: %s (%s)",
            exc,
            type(exc).__name__,
            exc_info=True,
        )
        return None
