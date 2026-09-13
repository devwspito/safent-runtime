"""Prepare Ads from the ONE Integrations vault, without user OAuth or secrets in UI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from hermes.integrations.composio.composio_client import ComposioApiError, ComposioClient
from hermes.shell_server.integrations.repo import SQLiteIntegrationsRepository
from hermes.shell_server.security.secrets import SecretsVault


async def prepare_composio_ads_configs(
    db_path: Path, *, client_factory: Any = ComposioClient,
) -> dict[str, bool]:
    """Resolve missing Google managed auth once; never auto-select a Meta app.

    Existing selections are revalidated by the Ads broker at connection time.
    Called by the owner endpoint and by the fixed-purpose lease publisher on
    upgrade. Remote failures do not disable unrelated integrations or prevent
    publishing a disabled/empty lease. Readiness here is CONFIG, not consent.
    """
    repo = SQLiteIntegrationsRepository(db_path=db_path, vault=SecretsVault())
    fingerprint = repo.credential_fingerprint()
    readiness = {"googleads": False, "metaads": False}
    if fingerprint is None:
        return readiness
    config_ids = repo.auth_config_ids()
    readiness.update({slug: bool(config_ids.get(slug)) for slug in readiness})
    if readiness["googleads"]:
        return readiness
    api_key = repo.reveal_api_key(kind="composio")
    if not api_key:
        return {"googleads": False, "metaads": False}
    client = client_factory(api_key, auth_config_ids=config_ids)
    try:
        config = await asyncio.wait_for(client.resolve_ads_auth_config("googleads"), timeout=10)
    except (ComposioApiError, TimeoutError):
        return readiness
    if config.toolkit_slug != "googleads" or config.status != "ENABLED":
        return readiness
    readiness["googleads"] = repo.set_auth_config_for_credential(
        toolkit_slug="googleads", auth_config_id=config.id,
        expected_fingerprint=fingerprint,
    )
    return readiness
