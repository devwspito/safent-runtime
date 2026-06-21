"""ComposioCredential from the integrations table in shell-state.db.

Mirrors the pattern of provider_config_source.py: import local (no
import-time cycle between runtime and shell_server), fail-soft → None,
consistent with "daemon cae degradado, no revienta".

The caller (composio_tool_specs.py) checks for None before building
Composio tools.  If Composio is not configured the OS keeps working with
native tools only.

NEVER log the api_key.  NEVER return it anywhere except to ComposioClient.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("hermes.runtime.composio_config")


@dataclass(frozen=True, slots=True)
class ComposioCredential:
    """Decrypted Composio credentials for one run-cycle."""

    api_key: str
    entity_id: str


def load_composio_credential(db_path: Path) -> ComposioCredential | None:
    """Return ComposioCredential from shell-state.db, or None.

    Fail-soft: any error (DB absent, master.key absent in CI, schema old,
    no key stored) returns None and is logged — the agent stays alive without
    Composio tools until the user configures a key.
    """
    try:
        from hermes.shell_server.integrations.repo import (  # noqa: PLC0415
            SQLiteIntegrationsRepository,
        )
        from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        logger.debug("hermes.composio_config.shell_server_unavailable")
        return None

    try:
        repo = SQLiteIntegrationsRepository(db_path=db_path, vault=SecretsVault())
        integration = repo.get_or_none(kind="composio")
    except Exception:  # noqa: BLE001
        logger.warning("hermes.composio_config.load_failed", exc_info=True)
        return None

    if integration is None or not integration.has_api_key or not integration.enabled:
        return None

    try:
        api_key = repo.reveal_api_key(kind="composio")
    except Exception:  # noqa: BLE001
        logger.warning("hermes.composio_config.reveal_failed", exc_info=True)
        return None

    if not api_key:
        return None

    logger.info(
        "hermes.composio_config.active",
        extra={"entity_id": integration.entity_id},
    )
    return ComposioCredential(api_key=api_key, entity_id=integration.entity_id)
