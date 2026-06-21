"""InMemoryNodeInstallationAdapter — testing-only.

Cumple ``NodeInstallationPort`` del spec 003 con storage en dict.

Cubre tests de Phase 2 Foundational sin requerir Postgres ni SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

# Re-importamos los tipos desde el contract de spec 003. Para que el
# import funcione fuera del paquete contracts/ usamos el path absoluto.
from hermes.agents_os.domain.always_on_policy import InstallProfile


class NodeInstallationNotFound(RuntimeError):
    pass


class NodeInstallationFingerprintConflict(RuntimeError):
    pass


class _Node:
    """Estructura mutable interna; expongo solo via métodos."""

    def __init__(
        self,
        *,
        node_installation_id: UUID,
        profile_kind: InstallProfile,
        operational_model: str,
        current_image_version: str,
        active_slot: str,
        hardware_fingerprint_aggregate: str,
        current_channel: str,
        arch: str,
    ) -> None:
        self.node_installation_id = node_installation_id
        self.installed_at = datetime.now(tz=UTC)
        self.profile_kind = profile_kind
        self.operational_model = operational_model
        self.current_image_version = current_image_version
        self.previous_image_version: str | None = None
        self.active_slot = active_slot
        self.hardware_fingerprint = hardware_fingerprint_aggregate
        self.current_channel = current_channel
        self.state = "provisioning"
        self.last_healthy_boot_at: datetime | None = None
        self.arch = arch


class InMemoryNodeInstallationAdapter:
    """Storage dict en memoria. NO thread-safe."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, _Node] = {}
        self._by_fingerprint: dict[str, UUID] = {}

    async def create(
        self,
        *,
        profile_kind: InstallProfile,
        operational_model: str,
        current_image_version: str,
        active_slot: str,
        hardware_fingerprint_aggregate: str,
        current_channel: str,
        arch: str,
    ) -> UUID:
        if hardware_fingerprint_aggregate in self._by_fingerprint:
            existing_id = self._by_fingerprint[hardware_fingerprint_aggregate]
            existing = self._by_id[existing_id]
            if existing.state in ("active", "draining", "provisioning"):
                raise NodeInstallationFingerprintConflict(
                    f"Ya existe NodeInstallation activa con fingerprint "
                    f"{hardware_fingerprint_aggregate[:16]}..."
                )
        node_id = uuid4()
        node = _Node(
            node_installation_id=node_id,
            profile_kind=profile_kind,
            operational_model=operational_model,
            current_image_version=current_image_version,
            active_slot=active_slot,
            hardware_fingerprint_aggregate=hardware_fingerprint_aggregate,
            current_channel=current_channel,
            arch=arch,
        )
        self._by_id[node_id] = node
        self._by_fingerprint[hardware_fingerprint_aggregate] = node_id
        return node_id

    async def get_state(self, *, node_installation_id: UUID) -> str:
        node = self._by_id.get(node_installation_id)
        if node is None:
            raise NodeInstallationNotFound()
        return node.state

    async def update_state(
        self,
        *,
        node_installation_id: UUID,
        new_state: str,
        cause: str,
    ) -> None:
        node = self._by_id.get(node_installation_id)
        if node is None:
            raise NodeInstallationNotFound()
        # Transiciones permitidas (mínimas para tests).
        allowed = {
            "provisioning": {"active", "decommissioned"},
            "active": {"draining", "decommissioned", "rolled_back"},
            "draining": {"active", "rolled_back", "decommissioned"},
            "rolled_back": {"active", "decommissioned"},
            "decommissioned": set(),
        }
        if new_state == node.state:
            return  # idempotente
        if new_state not in allowed.get(node.state, set()):
            raise ValueError(
                f"Transición no permitida: {node.state} → {new_state}"
            )
        node.state = new_state

    async def record_slot_promotion(
        self,
        *,
        node_installation_id: UUID,
        new_active_slot: str,
        new_image_version: str,
        previous_image_version: str,
    ) -> None:
        node = self._by_id.get(node_installation_id)
        if node is None:
            raise NodeInstallationNotFound()
        node.previous_image_version = previous_image_version
        node.current_image_version = new_image_version
        node.active_slot = new_active_slot

    async def record_rollback(self, *, node_installation_id: UUID) -> None:
        node = self._by_id.get(node_installation_id)
        if node is None:
            raise NodeInstallationNotFound()
        if node.previous_image_version is None:
            raise ValueError("No hay previous_image_version para rollback")
        # Swap.
        node.current_image_version, node.previous_image_version = (
            node.previous_image_version,
            node.current_image_version,
        )
        # Swap slot.
        node.active_slot = "slot_a" if node.active_slot == "slot_b" else "slot_b"
        node.state = "rolled_back"

    async def record_healthy_boot(
        self, *, node_installation_id: UUID, timestamp: datetime
    ) -> None:
        node = self._by_id.get(node_installation_id)
        if node is None:
            raise NodeInstallationNotFound()
        node.last_healthy_boot_at = timestamp
