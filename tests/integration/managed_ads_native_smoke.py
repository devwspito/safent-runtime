"""Pinned Hermes smoke: no network, no provider credentials, no LLM request."""

import asyncio
import json
import os
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes.capabilities.domain.ports import ConsentContext, ExecutionOutcome, ExecutionStatus
from hermes.config_sync.ads_policy_contract import AdsPolicySpec
from hermes.config_sync.policy_document import PolicyBundle, PolicyPayload, signing_bytes
from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore
from hermes.mcp.application.mcp_server_manager import McpServerManager
from hermes.mcp.domain.value_objects import McpServerId, ServerSlug, Transport, TrustLevel
from hermes.runtime.managed_ads_mcp import scoped_ads_factory
from hermes.runtime.managed_ads_policy import apply_signed_ads
from hermes.shell_server.security.secrets import SecretsVault


async def main():
    with TemporaryDirectory(prefix="ads-native-") as folder:
        root = Path(folder)
        os.environ["HERMES_HOME"] = folder
        os.environ["HOME"] = folder
        # A stale native entry must not be auto-discovered by the daemon's
        # AIAgent/model_tools import; only our scoped manager may register Ads.
        (root / "config.yaml").write_text(
            "mcp_servers:\n  safent-ads:\n    command: /must-not-spawn\n    args: []\n"
        )
        key = Ed25519PrivateKey.generate()
        org, instance = str(uuid4()), str(uuid4())
        store = SQLiteAssociationStore(
            db_path=root / "state.sqlite", vault=SecretsVault(master_key=b"s" * 32)
        )
        os.environ["HERMES_SHELL_DB"] = str(store.db_path)
        store.save(
            association=InstanceAssociation(
                instance_id=instance,
                tenant_id=org,
                paired_at=datetime.now(UTC).isoformat(),
                cloud_endpoint="https://enterprise.invalid",
                signing_pubkey_hex=key.public_key().public_bytes_raw().hex(),
                license={},
                last_applied_version=0,
                state="active",
            ),
            instance_secret="synthetic-unused",
        )
        data = dict(
            version=1,
            tenant_id=org,
            issued_at=datetime.now(UTC).isoformat(),
            payload=PolicyPayload(
                ads=AdsPolicySpec(
                    mode="managed",
                    instance_id=instance,
                    central_origin="https://ads.invalid",
                    bindings=[],
                ),
            ),
        )
        apply_signed_ads(
            store,
            PolicyBundle(
                **data, signature_hex=key.sign(signing_bytes(**data)).hex()
            ).model_dump_json(),
        )

        def forbidden_factory(_transport):
            raise AssertionError("Local MCP subprocess was selected")

        manager = McpServerManager(
            client_factory=forbidden_factory,
            scoped_client_factory=scoped_ads_factory(store.db_path, forbidden_factory),
        )
        server = await manager.connect(
            server_id=McpServerId("safent-ads"),
            slug=ServerSlug("safent-ads"),
            transport=Transport.stdio(["/must-not-spawn"]),
            trust_level=TrustLevel.MANAGED_REMOTE,
        )
        from tools.registry import registry

        from hermes.runtime.nous_engine import register_mcp_tools_in_nous_registry

        class DenyingBroker:
            calls = []

            async def dispatch(self, proposal, context):
                self.calls.append((proposal, context))
                return ExecutionOutcome(
                    proposal_id=proposal.proposal_id,
                    status=ExecutionStatus.REJECTED_BY_POLICY,
                    error="test-denial",
                )

        broker = DenyingBroker()
        context = ConsentContext(tenant_id=uuid4(), operator_id=uuid4())
        register_mcp_tools_in_nous_registry(server, broker, context, asyncio.get_running_loop())
        for tool in ("propose_pause", "propose_ad_child"):
            name = "mcp__safent-ads__" + tool
            assert registry.get_schema(name)["parameters"]["required"] == ["grant_id", "arguments"]
            args = {"grant_id": "explicit-grant", "arguments": {"entity_ref": "scoped"}}
            if tool == "propose_ad_child":
                args["arguments"]["child_plan"] = {
                    "schema_version": 1,
                    "platform": "meta",
                    "kind": "ad",
                    "status": "PAUSED",
                    "native": {"name": "Fixture child", "creative_id": "789"},
                }
            output = await asyncio.to_thread(registry.get_entry(name).handler, args)
            assert json.loads(output)["error"].startswith("mcp_read_blocked")
            assert broker.calls[-1][0].parameters["args"] == args
        assert len(broker.calls) == 2
        from tools.mcp_tool_common import _core

        assert "safent-ads" not in _core._servers
        from hermes.agents_os.infrastructure.dbus_runtime_service import _neus_write_mcp_entry

        before = (root / "config.yaml").read_text()
        try:
            _neus_write_mcp_entry("safent-ads", ["/must-not-spawn"])
        except PermissionError:
            pass
        else:
            raise AssertionError("Native configuration bypassed the signed mode")
        assert (root / "config.yaml").read_text() == before
        await manager.disconnect(McpServerId("safent-ads"))
        print(
            f"PASS Hermes {version('hermes-agent')}: 9 managed Ads schemas; "
            "pause/child exact args each reach broker once; no local discovery/subprocess/network"
        )


if __name__ == "__main__":
    asyncio.run(main())
