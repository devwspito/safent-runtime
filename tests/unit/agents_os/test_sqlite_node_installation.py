"""Tests SQLiteNodeInstallationAdapter (FR-003, FR-008)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes.agents_os.domain.always_on_policy import InstallProfile
from hermes.agents_os.infrastructure.sqlite_node_installation import (
    NodeInstallationFingerprintConflict,
    NodeInstallationNotFound,
    SQLiteNodeInstallationAdapter,
)

pytestmark = pytest.mark.unit

MIGRATION = (
    Path(__file__).parents[3]
    / "ops"
    / "agents-os-edition"
    / "migrations"
    / "sqlite"
    / "001_initial_personal_desktop.sql"
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    sql = MIGRATION.read_text(encoding="utf-8")
    conn = sqlite3.connect(db)
    conn.executescript(sql)
    conn.close()
    return db


@pytest.fixture
def adapter(db_path: Path) -> SQLiteNodeInstallationAdapter:
    return SQLiteNodeInstallationAdapter(db_path=db_path)


class TestCreate:
    async def test_create_returns_uuid(
        self, adapter: SQLiteNodeInstallationAdapter
    ) -> None:
        nid = await adapter.create(
            profile_kind=InstallProfile.PERSONAL_DESKTOP,
            operational_model="self_hosted",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="fp-abc",
            current_channel="stable",
            arch="aarch64",
        )
        state = await adapter.get_state(node_installation_id=nid)
        assert state == "provisioning"

    async def test_create_fingerprint_conflict_active(
        self, adapter: SQLiteNodeInstallationAdapter
    ) -> None:
        await adapter.create(
            profile_kind=InstallProfile.SERVER,
            operational_model="cloud_saas_managed",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="fp-conflict",
            current_channel="stable",
            arch="x86_64",
        )
        with pytest.raises(NodeInstallationFingerprintConflict):
            await adapter.create(
                profile_kind=InstallProfile.SERVER,
                operational_model="cloud_saas_managed",
                current_image_version="v1.0.0",
                active_slot="slot_a",
                hardware_fingerprint_aggregate="fp-conflict",
                current_channel="stable",
                arch="x86_64",
            )


class TestStateTransitions:
    async def test_provisioning_to_active(
        self, adapter: SQLiteNodeInstallationAdapter
    ) -> None:
        nid = await adapter.create(
            profile_kind=InstallProfile.PERSONAL_DESKTOP,
            operational_model="self_hosted",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="fp-1",
            current_channel="stable",
            arch="aarch64",
        )
        await adapter.update_state(
            node_installation_id=nid, new_state="active", cause="boot_healthy"
        )
        assert await adapter.get_state(node_installation_id=nid) == "active"

    async def test_invalid_transition_blocked(
        self, adapter: SQLiteNodeInstallationAdapter
    ) -> None:
        nid = await adapter.create(
            profile_kind=InstallProfile.PERSONAL_DESKTOP,
            operational_model="self_hosted",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="fp-2",
            current_channel="stable",
            arch="aarch64",
        )
        with pytest.raises(ValueError):
            await adapter.update_state(
                node_installation_id=nid,
                new_state="rolled_back",
                cause="boom",
            )

    async def test_get_state_unknown_node_raises(
        self, adapter: SQLiteNodeInstallationAdapter
    ) -> None:
        from uuid import uuid4

        with pytest.raises(NodeInstallationNotFound):
            await adapter.get_state(node_installation_id=uuid4())


class TestSlotPromotion:
    async def test_promotion_updates_versions(
        self, adapter: SQLiteNodeInstallationAdapter
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
            new_image_version="v1.0.1",
            previous_image_version="v1.0.0",
        )
        rec = adapter.fetch(nid)
        assert rec["current_image_version"] == "v1.0.1"
        assert rec["previous_image_version"] == "v1.0.0"
        assert rec["active_slot"] == "slot_b"

    async def test_rollback_swaps_versions_and_slots(
        self, adapter: SQLiteNodeInstallationAdapter
    ) -> None:
        nid = await adapter.create(
            profile_kind=InstallProfile.SERVER,
            operational_model="self_hosted",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="fp-4",
            current_channel="stable",
            arch="x86_64",
        )
        await adapter.record_slot_promotion(
            node_installation_id=nid,
            new_active_slot="slot_b",
            new_image_version="v1.0.1",
            previous_image_version="v1.0.0",
        )
        await adapter.record_rollback(node_installation_id=nid)
        rec = adapter.fetch(nid)
        assert rec["current_image_version"] == "v1.0.0"
        assert rec["previous_image_version"] == "v1.0.1"
        assert rec["active_slot"] == "slot_a"
        assert rec["state"] == "rolled_back"


class TestHealthyBoot:
    async def test_record_healthy_boot_sets_timestamp(
        self, adapter: SQLiteNodeInstallationAdapter
    ) -> None:
        nid = await adapter.create(
            profile_kind=InstallProfile.PERSONAL_DESKTOP,
            operational_model="self_hosted",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="fp-5",
            current_channel="stable",
            arch="aarch64",
        )
        ts = datetime(2026, 5, 28, 10, 30, 0, tzinfo=UTC)
        await adapter.record_healthy_boot(
            node_installation_id=nid, timestamp=ts
        )
        rec = adapter.fetch(nid)
        assert rec["last_healthy_boot_at"] == ts
