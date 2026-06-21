"""Tests PostgresNodeInstallationAdapter — fakes asyncpg.

Para tests reales contra Postgres ver tests/integration/.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.domain.always_on_policy import InstallProfile
from hermes.agents_os.infrastructure.postgres_node_installation import (
    NodeInstallationFingerprintConflict,
    NodeInstallationNotFound,
    PostgresNodeInstallationAdapter,
)

pytestmark = pytest.mark.unit


class _FakeConn:
    """Mínima fake asyncpg connection — guarda filas en dict."""

    def __init__(self, store: dict[UUID, dict]) -> None:
        self._store = store

    async def fetchrow(self, query: str, *args):
        q = " ".join(query.split())
        if "FROM agents_os.node_installations WHERE hardware_fingerprint" in q:
            fp = args[0]
            for row in self._store.values():
                if row["hardware_fingerprint"] == fp:
                    return row
            return None
        if "FROM agents_os.node_installations WHERE node_installation_id" in q:
            return self._store.get(args[0])
        raise NotImplementedError(query)

    async def execute(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO agents_os.node_installations"):
            (
                node_id,
                installed_at,
                profile_kind,
                operational_model,
                current_image_version,
                active_slot,
                hardware_fingerprint,
                current_channel,
                arch,
            ) = args
            self._store[node_id] = {
                "node_installation_id": node_id,
                "installed_at": installed_at,
                "profile_kind": profile_kind,
                "operational_model": operational_model,
                "current_image_version": current_image_version,
                "previous_image_version": None,
                "active_slot": active_slot,
                "hardware_fingerprint": hardware_fingerprint,
                "current_channel": current_channel,
                "state": "provisioning",
                "last_healthy_boot_at": None,
                "arch": arch,
            }
            return "INSERT 0 1"
        if q.startswith("UPDATE agents_os.node_installations SET state ="):
            new_state, node_id = args
            if node_id not in self._store:
                return "UPDATE 0"
            self._store[node_id]["state"] = new_state
            return "UPDATE 1"
        if "SET previous_image_version" in q and "current_image_version" in q:
            prev, curr, slot, node_id = args
            if node_id not in self._store:
                return "UPDATE 0"
            self._store[node_id]["previous_image_version"] = prev
            self._store[node_id]["current_image_version"] = curr
            self._store[node_id]["active_slot"] = slot
            return "UPDATE 1"
        if "state = 'rolled_back'" in q:
            curr, prev, slot, node_id = args
            self._store[node_id]["current_image_version"] = curr
            self._store[node_id]["previous_image_version"] = prev
            self._store[node_id]["active_slot"] = slot
            self._store[node_id]["state"] = "rolled_back"
            return "UPDATE 1"
        if "SET last_healthy_boot_at" in q:
            ts, node_id = args
            if node_id not in self._store:
                return "UPDATE 0"
            self._store[node_id]["last_healthy_boot_at"] = ts
            return "UPDATE 1"
        raise NotImplementedError(query)

    async def fetchval(self, query, *args):
        raise NotImplementedError


class _FakePool:
    def __init__(self) -> None:
        self.store: dict[UUID, dict] = {}

    def acquire(self):
        @asynccontextmanager
        async def _mgr():
            yield _FakeConn(self.store)

        return _mgr()


@pytest.fixture
def pool() -> _FakePool:
    return _FakePool()


@pytest.fixture
def adapter(pool: _FakePool) -> PostgresNodeInstallationAdapter:
    return PostgresNodeInstallationAdapter(pool=pool)


class TestCreate:
    async def test_create_returns_uuid(
        self, adapter: PostgresNodeInstallationAdapter
    ) -> None:
        nid = await adapter.create(
            profile_kind=InstallProfile.SERVER,
            operational_model="cloud_saas_managed",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="fp-srv",
            current_channel="stable",
            arch="x86_64",
        )
        assert await adapter.get_state(node_installation_id=nid) == "provisioning"

    async def test_fingerprint_conflict(
        self, adapter: PostgresNodeInstallationAdapter
    ) -> None:
        await adapter.create(
            profile_kind=InstallProfile.SERVER,
            operational_model="self_hosted",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="fp-dup",
            current_channel="stable",
            arch="x86_64",
        )
        with pytest.raises(NodeInstallationFingerprintConflict):
            await adapter.create(
                profile_kind=InstallProfile.SERVER,
                operational_model="self_hosted",
                current_image_version="v1.0.0",
                active_slot="slot_a",
                hardware_fingerprint_aggregate="fp-dup",
                current_channel="stable",
                arch="x86_64",
            )


class TestTransitions:
    async def test_provisioning_to_active(
        self, adapter: PostgresNodeInstallationAdapter
    ) -> None:
        nid = await adapter.create(
            profile_kind=InstallProfile.WORKSPACE_ONLY,
            operational_model="cloud_saas_managed",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="fp-1",
            current_channel="stable",
            arch="x86_64",
        )
        await adapter.update_state(
            node_installation_id=nid, new_state="active", cause="boot_ok"
        )
        assert await adapter.get_state(node_installation_id=nid) == "active"

    async def test_invalid_transition_blocked(
        self, adapter: PostgresNodeInstallationAdapter
    ) -> None:
        nid = await adapter.create(
            profile_kind=InstallProfile.SERVER,
            operational_model="self_hosted",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="fp-2",
            current_channel="stable",
            arch="x86_64",
        )
        with pytest.raises(ValueError):
            await adapter.update_state(
                node_installation_id=nid,
                new_state="rolled_back",
                cause="x",
            )


class TestSlotPromotion:
    async def test_rollback_swaps(
        self, adapter: PostgresNodeInstallationAdapter, pool: "_FakePool"
    ) -> None:
        nid = await adapter.create(
            profile_kind=InstallProfile.SERVER,
            operational_model="self_hosted",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="fp-3",
            current_channel="stable",
            arch="x86_64",
        )
        await adapter.record_slot_promotion(
            node_installation_id=nid,
            new_active_slot="slot_b",
            new_image_version="v1.1.0",
            previous_image_version="v1.0.0",
        )
        await adapter.record_rollback(node_installation_id=nid)
        rec = pool.store[nid]
        assert rec["current_image_version"] == "v1.0.0"
        assert rec["state"] == "rolled_back"
        assert rec["active_slot"] == "slot_a"

    async def test_record_healthy_boot_unknown_raises(
        self, adapter: PostgresNodeInstallationAdapter
    ) -> None:
        with pytest.raises(NodeInstallationNotFound):
            await adapter.record_healthy_boot(
                node_installation_id=uuid4(), timestamp=datetime.now(tz=UTC)
            )
