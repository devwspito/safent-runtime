"""SQLiteNodeInstallationAdapter — variante personal-desktop.

Cumple `NodeInstallationPort` contra una SQLite WAL local. Las tablas
ya están creadas por la migration `001_initial_personal_desktop.sql`
(spec 003 T027).

Sin SQLAlchemy — el storage personal-desktop es single-tenant, los
queries son triviales y queremos minimizar dependencias en la imagen
OCI bootc.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
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


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class SQLiteNodeInstallationAdapter:
    """SQLite WAL backend.

    Args:
        db_path: ruta al archivo SQLite. Se asume que la migration
            001_initial_personal_desktop.sql ya está aplicada.
    """

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            isolation_level=None,  # autocommit
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

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
        node_id = uuid4()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT node_installation_id, state FROM node_installations "
                "WHERE hardware_fingerprint = ?",
                (hardware_fingerprint_aggregate,),
            ).fetchone()
            if existing is not None and existing["state"] in (
                "active",
                "draining",
                "provisioning",
            ):
                raise NodeInstallationFingerprintConflict(
                    f"Ya existe NodeInstallation activa con fingerprint "
                    f"{hardware_fingerprint_aggregate[:16]}..."
                )
            conn.execute(
                """
                INSERT INTO node_installations (
                  node_installation_id, installed_at, profile_kind,
                  operational_model, current_image_version, active_slot,
                  hardware_fingerprint, current_channel, state, arch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'provisioning', ?)
                """,
                (
                    str(node_id),
                    _utc_now_iso(),
                    profile_kind.value.replace("-", "_"),
                    operational_model,
                    current_image_version,
                    active_slot,
                    hardware_fingerprint_aggregate,
                    current_channel,
                    arch,
                ),
            )
        return node_id

    async def get_state(self, *, node_installation_id: UUID) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM node_installations "
                "WHERE node_installation_id = ?",
                (str(node_installation_id),),
            ).fetchone()
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
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM node_installations "
                "WHERE node_installation_id = ?",
                (str(node_installation_id),),
            ).fetchone()
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
            conn.execute(
                "UPDATE node_installations SET state = ? "
                "WHERE node_installation_id = ?",
                (new_state, str(node_installation_id)),
            )

    async def record_slot_promotion(
        self,
        *,
        node_installation_id: UUID,
        new_active_slot: str,
        new_image_version: str,
        previous_image_version: str,
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE node_installations
                SET previous_image_version = ?,
                    current_image_version = ?,
                    active_slot = ?
                WHERE node_installation_id = ?
                """,
                (
                    previous_image_version,
                    new_image_version,
                    new_active_slot,
                    str(node_installation_id),
                ),
            )
            if cursor.rowcount == 0:
                raise NodeInstallationNotFound()

    async def record_rollback(self, *, node_installation_id: UUID) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT current_image_version, previous_image_version, "
                "active_slot FROM node_installations "
                "WHERE node_installation_id = ?",
                (str(node_installation_id),),
            ).fetchone()
            if row is None:
                raise NodeInstallationNotFound()
            if row["previous_image_version"] is None:
                raise ValueError("No hay previous_image_version para rollback")
            new_slot = (
                "slot_a" if row["active_slot"] == "slot_b" else "slot_b"
            )
            conn.execute(
                """
                UPDATE node_installations
                SET current_image_version = ?,
                    previous_image_version = ?,
                    active_slot = ?,
                    state = 'rolled_back'
                WHERE node_installation_id = ?
                """,
                (
                    row["previous_image_version"],
                    row["current_image_version"],
                    new_slot,
                    str(node_installation_id),
                ),
            )

    async def record_healthy_boot(
        self, *, node_installation_id: UUID, timestamp: datetime
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE node_installations "
                "SET last_healthy_boot_at = ? "
                "WHERE node_installation_id = ?",
                (timestamp.isoformat(), str(node_installation_id)),
            )
            if cursor.rowcount == 0:
                raise NodeInstallationNotFound()

    # Utilidades de testing/debug — no son parte del puerto.
    def fetch(self, node_installation_id: UUID) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM node_installations "
                "WHERE node_installation_id = ?",
                (str(node_installation_id),),
            ).fetchone()
            if row is None:
                raise NodeInstallationNotFound()
            return {
                **dict(row),
                "installed_at": _parse_iso(row["installed_at"]),
                "last_healthy_boot_at": _parse_iso(row["last_healthy_boot_at"]),
            }
