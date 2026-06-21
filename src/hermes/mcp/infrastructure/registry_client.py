"""mcp/infrastructure/registry_client — cliente async del MCP Registry oficial.

Fuente canónica: https://registry.modelcontextprotocol.io
Schema real confirmado contra la API v0 (2026-06-10):

  GET /v0/servers?[search=<q>&]limit=N
  → {"servers": [{"server": {name, description, title?, version, repository?,
                              packages?: [...], remotes?: [...]},
                  "_meta": {...}}, ...],
     "metadata": {"nextCursor": str, "count": int}}

  packages[i]: {registryType, identifier, version?, runtimeHint?,
                transport: {type}, runtimeArguments?: [{type, value}],
                environmentVariables?: [{name, description, isRequired?,
                                         isSecret?, default?}]}

Base URL configurable por HERMES_MCP_REGISTRY_URL (default prod).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("hermes.mcp.registry_client")

_DEFAULT_REGISTRY_URL = "https://registry.modelcontextprotocol.io"
_REQUEST_TIMEOUT = 15.0


class McpRegistryError(RuntimeError):
    """Raised when the MCP Registry HTTP call fails."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"MCP Registry {status_code}: {detail}")
        self.status_code = status_code


def _registry_base_url() -> str:
    return os.environ.get("HERMES_MCP_REGISTRY_URL", _DEFAULT_REGISTRY_URL).rstrip("/")


async def search_servers(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """GET /v0/servers from the official MCP Registry.

    Returns the raw list of server dicts (each a {"server": {...}, "_meta": {...}}).
    Raises McpRegistryError on HTTP or network failure.
    """
    import httpx  # deferred — keeps module importable without httpx installed  # noqa: PLC0415

    params: dict[str, str | int] = {"limit": limit}
    if query.strip():
        params["search"] = query.strip()

    url = f"{_registry_base_url()}/v0/servers"
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise McpRegistryError(504, f"timeout: {exc}") from exc
    except httpx.RequestError as exc:
        raise McpRegistryError(502, f"network error: {exc}") from exc

    if response.status_code != 200:
        raise McpRegistryError(response.status_code, response.text[:300])

    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        raise McpRegistryError(502, f"invalid JSON: {exc}") from exc

    return body.get("servers", [])
