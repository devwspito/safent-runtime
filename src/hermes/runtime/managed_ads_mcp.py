"""Ads client selection inside the existing MCP manager/capability broker."""

import json
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

from hermes.instance.association_store import SQLiteAssociationStore
from hermes.mcp.application.ports import McpClientPort
from hermes.mcp.domain.value_objects import ServerSlug, Transport
from hermes.runtime.managed_ads_policy import ManagedAdsUnavailable, read_ads_policy
from hermes.runtime.managed_ads_transport import TOOLS, ManagedAdsTransport


class AdsScopedClient:
    """An already-connected local client cannot survive an authority transition."""

    def __init__(
        self,
        db_path: Path,
        local_factory: Callable[[], McpClientPort],
        store_factory: Callable[[], SQLiteAssociationStore],
    ) -> None:
        self.db_path = db_path
        self.local_factory, self.store_factory = local_factory, store_factory
        self.local: McpClientPort | None = None
        self.mode: str | None = None

    def _mode(self) -> str:
        policy = read_ads_policy(self.db_path)
        return policy.mode if policy is not None else "free"

    def _check(self) -> None:
        if self.mode != self._mode():
            raise ManagedAdsUnavailable()

    async def initialize(self) -> None:
        self.mode = self._mode()
        if self.mode == "free":
            self.local = self.local_factory()
            await self.local.initialize()
            self._check()

    async def list_tools(self) -> list[dict[str, Any]]:
        self._check()
        if self.local is not None:
            return await self.local.list_tools()
        schemas = json.loads(
            files("hermes.runtime").joinpath("managed_ads_tool_schemas.json").read_text()
        )
        if set(schemas) != TOOLS:
            raise ManagedAdsUnavailable()

        def wrapped(name: str) -> dict[str, Any]:
            arguments = schemas[name]
            definitions = arguments.pop("$defs", {})
            return {
                "type": "object",
                "additionalProperties": False,
                "properties": {"grant_id": {"type": "string"}, "arguments": arguments},
                "required": ["grant_id", "arguments"],
                "$defs": definitions,
            }

        return [
            {
                "name": "list_managed_assignments",
                "description": "Asignaciones Ads firmadas; no prueba de acceso vigente.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            *[
                {
                    "name": name,
                    "description": "Cuenta Enterprise explícita. No aprueba ni ejecuta cambios.",
                    "inputSchema": wrapped(name),
                }
                for name in sorted(TOOLS)
            ],
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self._check()
        if self.local is not None:
            return await self.local.call_tool(name, args)
        if name == "list_managed_assignments" and args == {}:
            policy = read_ads_policy(self.db_path)
            if policy is None or policy.mode != "managed":
                raise ManagedAdsUnavailable()
            result = {"bindings": [item.model_dump() for item in policy.bindings]}
        else:
            if not isinstance(args, dict) or set(args) != {"grant_id", "arguments"}:
                raise ManagedAdsUnavailable()
            result = await ManagedAdsTransport(self.store_factory()).call(
                args["grant_id"], name, args["arguments"]
            )
        return {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False}

    async def close(self) -> None:
        if self.local is not None:
            await self.local.close()
        self.local = None
        self.mode = None


def scoped_ads_factory(
    db_path: Path, local_factory: Callable[[Transport], McpClientPort]
) -> Callable[[ServerSlug, Transport], McpClientPort]:
    def choose(slug: ServerSlug, transport: Transport) -> McpClientPort:
        if str(slug) != "safent-ads":
            return local_factory(transport)

        def store() -> SQLiteAssociationStore:
            from hermes.instance.association_store import SQLiteAssociationStore  # noqa: PLC0415
            from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415

            return SQLiteAssociationStore(db_path=db_path, vault=SecretsVault())

        return AdsScopedClient(db_path, lambda: local_factory(transport), store)

    return choose
