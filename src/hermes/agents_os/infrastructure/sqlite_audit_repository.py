"""SqliteAuditRepository — persiste AuditEntry en audit_chain_entries (CTRL-7/CTRL-9).

Implementa SignedAuditRepositoryPort de capabilities/domain/ports.py.

Patrón: conexión por llamada, autocommit (isolation_level=None), DDL idempotente
on-connect vía ensure_audit_chain_schema (fuente de verdad del DDL — T010).
row_factory sqlite3.Row — igual a SQLiteConsentRepository.

head_hash_hex() siembra _last_hash del AuditHashChainSigner al reiniciar
para no romper la cadena cross-restart (corrige regresión AUD-1).

RECONCILIACIÓN DDL (T010): usa ensure_audit_chain_schema en lugar de _DDL propio.
La tabla tiene `seq` AUTOINCREMENT como PK de orden total; head_hash_hex()
ordena por `seq DESC` (robusto ante timestamps iguales).

T042 — ExternalAnchorPort opcional: si se inyecta, se llama a anchor() tras
cada append exitoso (CTRL-8/AUD-2). La falta de anchor no bloquea el append.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from hermes.agents_os.application.audit_hash_chain import AuditEntry, AuditKind
from hermes.agents_os.infrastructure.audit_schema import ensure_audit_chain_schema

if TYPE_CHECKING:
    from hermes.capabilities.application.external_anchor import ExternalAnchorPort

logger = logging.getLogger("hermes.agents_os.audit_repo")


class SqliteAuditRepository:
    """Repositorio append-only de AuditEntry firmadas sobre SQLite.

    Thread-safe: cada llamada abre y cierra su propia conexión (patrón consent repo).
    DDL: delega en ensure_audit_chain_schema (fuente de verdad T010).

    Args:
        db_path:         Ruta al fichero SQLite.
        external_anchor: Si se inyecta, se llama a anchor(head_hash) tras cada
                         append exitoso (CTRL-8/AUD-2). Opcional en P0.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        external_anchor: ExternalAnchorPort | None = None,
    ) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._anchor = external_anchor
        self._ensure_schema()

    def set_external_anchor(self, anchor: "ExternalAnchorPort") -> None:
        """Wire the external WORM/TSA anchor post-construction.

        V-6: the anchor (CompositeExternalAnchor) is built later in the boot
        sequence than this repo, so it could not be passed to __init__. Without
        it, _try_anchor() is a silent no-op and a daemon-RCE can rewrite the whole
        hash chain undetected (verify re-derives with the same in-proc key). This
        setter lets the boot wiring attach the anchor once it exists.
        """
        self._anchor = anchor

    # ------------------------------------------------------------------
    # SignedAuditRepositoryPort
    # ------------------------------------------------------------------

    async def append(self, entry: AuditEntry) -> None:
        """Persiste una AuditEntry firmada. Append-only: INSERT OR IGNORE.

        INSERT OR IGNORE garantiza idempotencia: un entry_id ya persistido
        no se sobreescribe (append-only por diseño).
        `created_at` = instante de persistencia (≠ timestamp lógico del firmer).

        Tras persistir, si external_anchor está inyectado, llama a
        anchor(signed_payload_hash_hex) para anclar externamente (CTRL-8/AUD-2).
        El fallo del anchor NO bloquea el append — el error se silencia
        (degradación aceptable en P0: la cadena local sigue siendo correcta).
        """
        created_at = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO audit_chain_entries (
                    entry_id, node_installation_id, tenant_id,
                    timestamp, actor, audit_kind, category, description,
                    payload_hash_hex, prev_entry_hash_hex,
                    signed_payload_hash_hex, signature_hex, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(entry.entry_id),
                    str(entry.node_installation_id) if entry.node_installation_id else None,
                    str(entry.tenant_id) if entry.tenant_id else None,
                    entry.timestamp.isoformat(),
                    entry.actor,
                    str(entry.audit_kind),
                    entry.category,
                    entry.description,
                    entry.payload_hash_hex,
                    entry.prev_entry_hash_hex,
                    entry.signed_payload_hash_hex,
                    entry.signature_hex,
                    created_at,
                ),
            )
        self._try_project_to_view(entry)
        await self._try_anchor(entry.signed_payload_hash_hex)

    async def _try_anchor(self, head_hash_hex: str) -> None:
        """Ancla el head_hash externamente si hay anchor inyectado (CTRL-8).

        Fail-open: el error del anchor NO detiene el append. La cadena local
        sigue siendo correcta. verify() detectará la divergencia en el próximo ciclo.
        """
        if self._anchor is None:
            return
        try:
            await self._anchor.anchor(head_hash_hex)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.audit_repo.anchor_failed: %s — "
                "cadena local intacta, ancla externa no aplicada",
                exc,
            )

    async def head_hash_hex(self) -> str | None:
        """Hash (signed_payload_hash_hex) de la última entrada persistida.

        Siembra `_last_hash` del AuditHashChainSigner al reiniciar,
        corrigiendo la regresión AUD-1 (cadena rota entre reinicios).
        Ordena por `seq DESC` (monótono, robusto ante timestamps iguales).
        Returns None si la tabla está vacía (primer arranque).
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT signed_payload_hash_hex FROM audit_chain_entries "
                "ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return row["signed_payload_hash_hex"] if row else None

    async def load_chain(self, *, tenant_id: UUID | None = None) -> list[AuditEntry]:
        """Carga la cadena ordenada ascendentemente para verify_chain.

        Si tenant_id se especifica, filtra por ese tenant.
        Ordena por `seq ASC` (orden de inserción canónico — T010).
        """
        with self._connect() as conn:
            if tenant_id is not None:
                rows = conn.execute(
                    "SELECT * FROM audit_chain_entries "
                    "WHERE tenant_id = ? "
                    "ORDER BY seq ASC",
                    (str(tenant_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_chain_entries ORDER BY seq ASC"
                ).fetchall()
        return [_row_to_entry(r) for r in rows]

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL + NORMAL como las otras DBs (work-queue, conversation). Sin esto la
        # audit DB corría rollback-journal + synchronous=FULL → 2-3 fsync completos
        # por ciclo de chat. WAL amortiza el fsync; busy_timeout evita "database is
        # locked" bajo escritura concurrente del audit-tail.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            ensure_audit_chain_schema(conn)

    def _try_project_to_view(self, entry: AuditEntry) -> None:
        """Proyecta la entrada a audit_entries_view si la tabla existe (compatibilidad API).

        La vista original de audit_api.py tiene un esquema reducido (sin hash/firma).
        INSERT OR IGNORE para no fallar si ya existe o la vista no está creada.
        """
        with contextlib.suppress(sqlite3.OperationalError), self._connect() as conn:
            conn.execute(
                """
                    INSERT OR IGNORE INTO audit_entries_view (
                        entry_id, timestamp, actor, audit_kind,
                        category, description, signature_short
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    str(entry.entry_id),
                    entry.timestamp.isoformat(),
                    entry.actor,
                    str(entry.audit_kind),
                    entry.category,
                    entry.description,
                    entry.signature_hex[:16] + "…",
                ),
            )


# ---------------------------------------------------------------------------
# Row mapper
# ---------------------------------------------------------------------------


def _row_to_entry(row: sqlite3.Row) -> AuditEntry:
    def _uuid(val: str | None) -> UUID | None:
        return UUID(val) if val else None

    return AuditEntry(
        entry_id=UUID(row["entry_id"]),
        node_installation_id=_uuid(row["node_installation_id"]),
        tenant_id=_uuid(row["tenant_id"]),
        timestamp=datetime.fromisoformat(row["timestamp"]),
        actor=row["actor"],
        audit_kind=AuditKind(row["audit_kind"]),
        category=row["category"],
        description=row["description"],
        payload_hash_hex=row["payload_hash_hex"],
        prev_entry_hash_hex=row["prev_entry_hash_hex"],
        signed_payload_hash_hex=row["signed_payload_hash_hex"],
        signature_hex=row["signature_hex"],
    )
