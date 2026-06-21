"""Tests InMemoryNodeInstallationAdapter — spec 003 data-model §4."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hermes.agents_os.domain.always_on_policy import InstallProfile
from hermes.agents_os.testing.in_memory_node_installation import (
    InMemoryNodeInstallationAdapter,
    NodeInstallationFingerprintConflict,
    NodeInstallationNotFound,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def _make() -> tuple[InMemoryNodeInstallationAdapter, type]:
    return InMemoryNodeInstallationAdapter(), InstallProfile


class TestCreate:
    async def test_create_personal_desktop(self) -> None:
        repo, _ = await _make()
        node_id = await repo.create(
            profile_kind=InstallProfile.PERSONAL_DESKTOP,
            operational_model="self_hosted",
            current_image_version="agents-os-v0.1.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="hash-laptop-1",
            current_channel="stable",
            arch="x86_64",
        )
        assert node_id is not None
        assert await repo.get_state(node_installation_id=node_id) == "provisioning"

    async def test_fingerprint_conflict_blocks_duplicate(self) -> None:
        repo, _ = await _make()
        await repo.create(
            profile_kind=InstallProfile.SERVER,
            operational_model="self_hosted",
            current_image_version="v1",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="hash-server-1",
            current_channel="stable",
            arch="x86_64",
        )
        with pytest.raises(NodeInstallationFingerprintConflict):
            await repo.create(
                profile_kind=InstallProfile.SERVER,
                operational_model="self_hosted",
                current_image_version="v1",
                active_slot="slot_a",
                hardware_fingerprint_aggregate="hash-server-1",
                current_channel="stable",
                arch="x86_64",
            )

    async def test_get_state_unknown_raises(self) -> None:
        repo, _ = await _make()
        import uuid

        with pytest.raises(NodeInstallationNotFound):
            await repo.get_state(node_installation_id=uuid.uuid4())


class TestStateTransitions:
    async def test_provisioning_to_active(self) -> None:
        repo, _ = await _make()
        node_id = await repo.create(
            profile_kind=InstallProfile.WORKSPACE_ONLY,
            operational_model="cloud_saas_managed",
            current_image_version="v1",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="hash-vm-1",
            current_channel="stable",
            arch="x86_64",
        )
        await repo.update_state(
            node_installation_id=node_id,
            new_state="active",
            cause="first_healthy_boot",
        )
        assert await repo.get_state(node_installation_id=node_id) == "active"

    async def test_active_to_draining_and_back(self) -> None:
        repo, _ = await _make()
        node_id = await repo.create(
            profile_kind=InstallProfile.SERVER,
            operational_model="self_hosted",
            current_image_version="v1",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="hash-srv-2",
            current_channel="stable",
            arch="x86_64",
        )
        await repo.update_state(
            node_installation_id=node_id, new_state="active", cause="boot"
        )
        await repo.update_state(
            node_installation_id=node_id, new_state="draining", cause="ota"
        )
        await repo.update_state(
            node_installation_id=node_id, new_state="active", cause="ota_ok"
        )
        assert await repo.get_state(node_installation_id=node_id) == "active"

    async def test_decommissioned_is_terminal(self) -> None:
        repo, _ = await _make()
        node_id = await repo.create(
            profile_kind=InstallProfile.PERSONAL_DESKTOP,
            operational_model="self_hosted",
            current_image_version="v1",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="hash-end-1",
            current_channel="stable",
            arch="x86_64",
        )
        await repo.update_state(
            node_installation_id=node_id, new_state="decommissioned", cause="admin"
        )
        with pytest.raises(ValueError, match="Transición no permitida"):
            await repo.update_state(
                node_installation_id=node_id, new_state="active", cause="oops"
            )


class TestOtaSlotPromotion:
    async def test_promotion_swap_versions(self) -> None:
        repo, _ = await _make()
        node_id = await repo.create(
            profile_kind=InstallProfile.WORKSPACE_ONLY,
            operational_model="cloud_saas_managed",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="hash-ota-1",
            current_channel="stable",
            arch="x86_64",
        )
        await repo.record_slot_promotion(
            node_installation_id=node_id,
            new_active_slot="slot_b",
            new_image_version="v1.1.0",
            previous_image_version="v1.0.0",
        )

    async def test_rollback_restores_previous(self) -> None:
        repo, _ = await _make()
        node_id = await repo.create(
            profile_kind=InstallProfile.SERVER,
            operational_model="self_hosted",
            current_image_version="v1.0.0",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="hash-roll-1",
            current_channel="stable",
            arch="x86_64",
        )
        await repo.update_state(
            node_installation_id=node_id, new_state="active", cause="boot"
        )
        await repo.record_slot_promotion(
            node_installation_id=node_id,
            new_active_slot="slot_b",
            new_image_version="v1.1.0",
            previous_image_version="v1.0.0",
        )
        await repo.record_rollback(node_installation_id=node_id)
        assert await repo.get_state(node_installation_id=node_id) == "rolled_back"

    async def test_rollback_without_previous_raises(self) -> None:
        repo, _ = await _make()
        node_id = await repo.create(
            profile_kind=InstallProfile.SERVER,
            operational_model="self_hosted",
            current_image_version="v1",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="hash-noroll-1",
            current_channel="stable",
            arch="x86_64",
        )
        with pytest.raises(ValueError, match="No hay previous_image_version"):
            await repo.record_rollback(node_installation_id=node_id)


class TestHealthyBoot:
    async def test_record_healthy_boot(self) -> None:
        repo, _ = await _make()
        node_id = await repo.create(
            profile_kind=InstallProfile.WORKSPACE_ONLY,
            operational_model="cloud_saas_managed",
            current_image_version="v1",
            active_slot="slot_a",
            hardware_fingerprint_aggregate="hash-hb-1",
            current_channel="stable",
            arch="x86_64",
        )
        ts = datetime.now(tz=UTC)
        await repo.record_healthy_boot(
            node_installation_id=node_id, timestamp=ts
        )
        # No exception = OK.
