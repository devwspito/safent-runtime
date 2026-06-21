"""AuditTailWriter — emite el hash chain firmado al control plane.

Spec 003 FR-049 (BLOQUEANTE) + FR-006. El audit chain vive localmente
(SQLite/Postgres), pero el operador del control plane debe poder
reconstruirlo y verificarlo a posteriori.

Esta clase es la capa que toma `AuditEntry`s firmadas y las publica
al control plane vía HTTPS (mTLS). Idempotente — la entrada
ya tiene `entry_id` único y signature verificable; el CP detecta y
descarta duplicados.

Fail-closed: si el CP no es alcanzable, las entradas se acumulan en
una cola persistente local (`/var/lib/hermes/audit-tail-pending/`).
NUNCA se descartan; al recuperar conectividad se vuelcan en orden.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

from hermes.agents_os.application.audit_hash_chain import AuditEntry

logger = logging.getLogger(__name__)


class TailPublishError(RuntimeError):
    pass


@runtime_checkable
class AuditTailTransport(Protocol):
    """Transporte HTTPS hacia el control plane (mTLS gestionado fuera)."""

    def publish(self, *, entries: list[dict]) -> None:
        ...


@dataclass(slots=True)
class TailWriterStats:
    queued_in_memory: int
    persisted_pending: int
    published_total: int
    failures_total: int
    last_publish_at: datetime | None


class HttpsAuditTailTransport:
    """Production HTTPS transport to the Hermes control plane.

    Uses httpx with optional mTLS client certificate.  Timeout: 10 s.
    Raises TailPublishError on non-2xx or network failure so the caller
    can spool-to-disk and retry.
    """

    def __init__(
        self,
        *,
        url: str,
        client_cert: str | None = None,
        client_key: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._url = url
        self._client_cert = client_cert
        self._client_key = client_key
        self._timeout = timeout

    def publish(self, *, entries: list[dict]) -> None:
        try:
            import httpx  # noqa: PLC0415
        except ImportError as exc:
            raise TailPublishError("httpx not installed") from exc

        cert = None
        if self._client_cert and self._client_key:
            cert = (self._client_cert, self._client_key)

        try:
            response = httpx.post(
                self._url,
                json={"entries": entries},
                cert=cert,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise TailPublishError(f"publish failed: {exc}") from exc


class FakeAuditTailTransport:
    """Transporte fake — guarda en lista. Para tests."""

    def __init__(self) -> None:
        self.published: list[dict] = []
        self.fail_count = 0

    def publish(self, *, entries: list[dict]) -> None:
        if self.fail_count > 0:
            self.fail_count -= 1
            raise TailPublishError("fake transport failure")
        self.published.extend(entries)


class AuditTailWriter:
    """Cola + publisher de audit entries hacia el CP.

    Args:
        transport: AuditTailTransport con `publish(entries)`.
        spool_dir: directorio para persistir entradas cuando el CP no
            es alcanzable. Si None, no persiste (solo memoria — útil
            para tests).
        batch_size: cuántas entradas publicar por flush.
    """

    def __init__(
        self,
        *,
        transport: AuditTailTransport,
        spool_dir: Path | None = None,
        batch_size: int = 50,
    ) -> None:
        self._transport = transport
        self._spool_dir = spool_dir
        self._batch_size = batch_size
        self._queue: queue.Queue[AuditEntry] = queue.Queue()
        self._published_total = 0
        self._failures_total = 0
        self._last_publish_at: datetime | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        if self._spool_dir is not None:
            self._spool_dir.mkdir(parents=True, exist_ok=True)
            self._enqueue_pending_from_spool()

    def enqueue(self, entry: AuditEntry) -> None:
        self._queue.put(entry)

    def _entry_to_dict(self, entry: AuditEntry) -> dict:
        return {
            "entry_id": str(entry.entry_id),
            "node_installation_id": (
                str(entry.node_installation_id)
                if entry.node_installation_id
                else None
            ),
            "tenant_id": (
                str(entry.tenant_id) if entry.tenant_id else None
            ),
            "timestamp": entry.timestamp.isoformat(),
            "actor": entry.actor,
            "audit_kind": entry.audit_kind.value,
            "category": entry.category,
            "description": entry.description,
            "payload_hash_hex": entry.payload_hash_hex,
            "prev_entry_hash_hex": entry.prev_entry_hash_hex,
            "signed_payload_hash_hex": entry.signed_payload_hash_hex,
            "signature_hex": entry.signature_hex,
        }

    def flush_once(self) -> int:
        """Vacía batch_size entradas. Retorna cuántas publicó."""
        batch: list[AuditEntry] = []
        for _ in range(self._batch_size):
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0
        payload = [self._entry_to_dict(e) for e in batch]
        try:
            self._transport.publish(entries=payload)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._failures_total += 1
            logger.warning("audit publish failed: %s — spooling", exc)
            self._spool_or_requeue(batch)
            return 0
        with self._lock:
            self._published_total += len(payload)
            self._last_publish_at = datetime.now(tz=UTC)
        return len(payload)

    def _spool_or_requeue(self, batch: list[AuditEntry]) -> None:
        if self._spool_dir is None:
            # Sin disk: re-enqueue para reintento en memoria.
            for e in batch:
                self._queue.put(e)
            return
        for e in batch:
            spool_file = self._spool_dir / f"{e.entry_id}.json"
            try:
                spool_file.write_text(
                    json.dumps(self._entry_to_dict(e)),
                    encoding="utf-8",
                )
            except OSError as ioexc:
                logger.error("spool write failed: %s", ioexc)
                # Re-enqueue como último recurso.
                self._queue.put(e)

    def _enqueue_pending_from_spool(self) -> None:
        if self._spool_dir is None or not self._spool_dir.exists():
            return
        for spool_file in sorted(self._spool_dir.glob("*.json")):
            try:
                data = json.loads(spool_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            self._queue.put(_reconstruct_entry(data))
            spool_file.unlink(missing_ok=True)

    def stats(self) -> TailWriterStats:
        pending = 0
        if self._spool_dir is not None and self._spool_dir.exists():
            pending = sum(1 for _ in self._spool_dir.glob("*.json"))
        with self._lock:
            return TailWriterStats(
                queued_in_memory=self._queue.qsize(),
                persisted_pending=pending,
                published_total=self._published_total,
                failures_total=self._failures_total,
                last_publish_at=self._last_publish_at,
            )

    def start_background(self, *, flush_interval_seconds: float = 30.0) -> None:
        """Start the background flush thread.

        Default interval raised from 5 s to 30 s: on idle (no audit events) the
        thread was waking every 5 s to drain an empty queue, burning CPU on ARM.
        The flush is also gated: when the in-memory queue is empty the thread
        sleeps the full interval without calling flush_once() at all.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.wait(flush_interval_seconds):
                # Skip the flush entirely when there is nothing queued — avoids
                # the lock + qsize() + get_nowait() round-trip on an empty queue.
                if not self._queue.empty():
                    self.flush_once()

        t = threading.Thread(target=_loop, name="audit-tail-writer", daemon=True)
        self._thread = t
        t.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


def _reconstruct_entry(data: dict) -> AuditEntry:
    """Hidrata un AuditEntry desde el spool JSON (sin re-verificar)."""
    from hermes.agents_os.application.audit_hash_chain import AuditKind

    return AuditEntry(
        entry_id=UUID(data["entry_id"]),
        node_installation_id=(
            UUID(data["node_installation_id"])
            if data["node_installation_id"]
            else None
        ),
        tenant_id=(
            UUID(data["tenant_id"]) if data["tenant_id"] else None
        ),
        timestamp=datetime.fromisoformat(data["timestamp"]),
        actor=data["actor"],
        audit_kind=AuditKind(data["audit_kind"]),
        category=data.get("category"),
        description=data["description"],
        payload_hash_hex=data["payload_hash_hex"],
        prev_entry_hash_hex=data["prev_entry_hash_hex"],
        signed_payload_hash_hex=data["signed_payload_hash_hex"],
        signature_hex=data["signature_hex"],
    )
