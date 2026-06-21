"""PostgresNodeInstallationAdapter — variante server/workspace_only.

Cumple `NodeInstallationPort` contra Postgres usando asyncpg directo.
Las tablas viven en el schema `agents_os` y ya están creadas por las
migrations 013-022 (spec 003 T025-T026).

Sin SQLAlchemy — server multi-tenant ya tiene asyncpg como dependencia
del control plane. Mantenemos un pool externo inyectable para evitar
crear conexiones en cada llamada.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from hermes.agents_os.domain.always_on_policy import InstallProfile


class NodeInstallationNotFound(RuntimeError):
    pass


class NodeInstallationFingerprintConflict(RuntimeError):
    pass


_VALID_TRANSITIONS = {
    "provisioning": {"active", "decommissioned"},
    "active": {"draining", "decommissioned", "rolled_back"},
    "draining": {"active", "rolled_back", "decommissioned"},
    "rolled_back": {"active", "decommissioned"},
    "decommissioned": set(),
}


@runtime_checkable
class _AsyncpgConnection(Protocol):
    """Subset de asyncpg.Connection / pool conn que usamos."""

    async def fetchrow(self, query: str, *args: Any) -> Any: ...

    async def fetchval(self, query: str, *args: Any) -> Any: ...

    async def execute(self, query: str, *args: Any) -> Any: ...


@runtime_checkable
class _AsyncpgPool(Protocol):
    def acquire(self) -> Any: ...


class PostgresNodeInstallationAdapter:
    """Postgres backend.

    Args:
        pool: pool asyncpg compatible (acquire context manager).
    """

    def __init__(self, *, pool: _AsyncpgPool) -> None:
        self._pool = pool

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
        normalized_profile = profile_kind.value.replace("-", "_")
        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT node_installation_id, state "
                "FROM agents_os.node_installations "
                "WHERE hardware_fingerprint = $1",
                hardware_fingerprint_aggregate,
            )
            if existing is not None and existing["state"] in (
                "active",
                "draining",
                "provisioning",
            ):
                raise NodeInstallationFingerprintConflict(
                    f"Ya existe NodeInstallation activa con fingerprint "
                    f"{hardware_fingerprint_aggregate[:16]}..."
                )
            node_id = uuid4()
            await conn.execute(
                """
                INSERT INTO agents_os.node_installations (
                  node_installation_id, installed_at, profile_kind,
                  operational_model, current_image_version, active_slot,
                  hardware_fingerprint, current_channel, state, arch
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'provisioning', $9)
                """,
                node_id,
                datetime.now(tz=UTC),
                normalized_profile,
                operational_model,
                current_image_version,
                active_slot,
                hardware_fingerprint_aggregate,
                current_channel,
                arch,
            )
            return node_id

    async def get_state(self, *, node_installation_id: UUID) -> str:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state FROM agents_os.node_installations "
                "WHERE node_installation_id = $1",
                node_installation_id,
            )
            if row is None:
                raise NodeInstallationNotFound()
            return row["state"]

    async def update_state(
        self,
        *,
        node_installation_id: UUID,
        new_state: str,
        cause: str,
    ) -> None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state FROM agents_os.node_installations "
                "WHERE node_installation_id = $1",
                node_installation_id,
            )
            if row is None:
                raise NodeInstallationNotFound()
            current = row["state"]
            if new_state == current:
                return
            allowed = _VALID_TRANSITIONS.get(current, set())
            if new_state not in allowed:
                raise ValueError(
                    f"Transición no permitida: {current} → {new_state}"
                )
            await conn.execute(
                "UPDATE agents_os.node_installations SET state = $1 "
                "WHERE node_installation_id = $2",
                new_state,
                node_installation_id,
            )

    async def record_slot_promotion(
        self,
        *,
        node_installation_id: UUID,
        new_active_slot: str,
        new_image_version: str,
        previous_image_version: str,
    ) -> None:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE agents_os.node_installations
                SET previous_image_version = $1,
                    current_image_version = $2,
                    active_slot = $3
                WHERE node_installation_id = $4
                """,
                previous_image_version,
                new_image_version,
                new_active_slot,
                node_installation_id,
            )
            if result.endswith("0"):
                raise NodeInstallationNotFound()

    async def record_rollback(self, *, node_installation_id: UUID) -> None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT current_image_version, previous_image_version, "
                "active_slot FROM agents_os.node_installations "
                "WHERE node_installation_id = $1",
                node_installation_id,
            )
            if row is None:
                raise NodeInstallationNotFound()
            if row["previous_image_version"] is None:
                raise ValueError(
                    "No hay previous_image_version para rollback"
                )
            new_slot = (
                "slot_a" if row["active_slot"] == "slot_b" else "slot_b"
            )
            await conn.execute(
                """
                UPDATE agents_os.node_installations
                SET current_image_version = $1,
                    previous_image_version = $2,
                    active_slot = $3,
                    state = 'rolled_back'
                WHERE node_installation_id = $4
                """,
                row["previous_image_version"],
                row["current_image_version"],
                new_slot,
                node_installation_id,
            )

    async def record_healthy_boot(
        self, *, node_installation_id: UUID, timestamp: datetime
    ) -> None:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE agents_os.node_installations "
                "SET last_healthy_boot_at = $1 "
                "WHERE node_installation_id = $2",
                timestamp,
                node_installation_id,
            )
            if result.endswith("0"):
                raise NodeInstallationNotFound()
