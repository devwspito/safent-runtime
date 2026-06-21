"""AuditHashChainSigner — registro audit hash-chain firmado (FR-049 BLOQUEANTE).

Threat-model SURF-AUD-01: prevenir tampering del audit log local.

Cada entrada se firma con HMAC-SHA-256 sobre:
    canonical_payload || prev_entry_hash

`prev_entry_hash` = signed_payload_hash de la entrada anterior. La
primera entrada usa `b"\\x00" * 32` (32 zeros) como ancla.

NUNCA se reescriben entradas pasadas — append-only. La verificación
recorre la cadena desde el génesis hasta `entry_id`; un solo hash roto
delata el tampering.

PII tokenization — el llamador es responsable de pasar payload SIN
PII en claro (constitución III). Aquí solo firmamos.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

_GENESIS_PREV_HASH = b"\x00" * 32


class AuditKind(StrEnum):
    """Categorías del audit log (research §12 + threat-model)."""

    CONSENT_GRANTED = "consent_granted"
    CONSENT_REVOKED = "consent_revoked"
    OTA_QUEUED = "ota_queued"
    OTA_PROMOTED = "ota_promoted"
    OTA_ROLLED_BACK = "ota_rolled_back"
    OTA_REJECTED = "ota_rejected"
    REMOTE_CONTROL_ISSUED = "remote_control_issued"
    REMOTE_CONTROL_ACCEPTED = "remote_control_accepted"
    REMOTE_CONTROL_ENDED = "remote_control_ended"
    NODE_INSTALL_CREATED = "node_install_created"
    NODE_INSTALL_STATE_CHANGED = "node_install_state_changed"
    SUSPEND_ATTEMPTED = "suspend_attempted"
    SUSPEND_DENIED = "suspend_denied"
    SKILL_PROMOTED = "skill_promoted"
    TENANT_BOUND = "tenant_bound"
    TENANT_REVOKED = "tenant_revoked"
    LANDLOCK_RULESET_APPLIED = "landlock_ruleset_applied"
    # --- feature 005: loop autónomo (append-only, no rompe valores existentes) ---
    TASK_ENQUEUED = "task_enqueued"
    TASK_CLAIMED = "task_claimed"
    PROPOSAL_EXECUTED = "proposal_executed"
    PROPOSAL_REJECTED = "proposal_rejected"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    AGENT_PAUSED = "agent_paused"
    AGENT_RESUMED = "agent_resumed"
    HITL_APPROVED = "hitl_approved"
    HITL_REJECTED = "hitl_rejected"
    # --- feature 006: control-plane D-Bus (append-only) ---
    WORKITEM_ACCEPTED = "workitem_accepted"
    # --- feature 007: trigger chain + auto-disparo (append-only, CTRL-P2-14) ---
    # Cadena no-repudio: TRIGGER_AUTHORIZED -> TRIGGER_ACTIVATED -> PROPOSAL_EXECUTED
    TRIGGER_AUTHORIZED = "trigger_authorized"   # admin firma autorización de origen
    TRIGGER_ACTIVATED = "trigger_activated"     # origen autorizado dispara trabajo
    TRIGGER_DENIED = "trigger_denied"           # intento de auto-disparo sin autorización
    # --- feature 006 / T051-fix: chat conversacional sin proposals (append-only) ---
    CHAT_REPLIED = "chat_replied"               # el agente respondió en texto sin ejecutar tools
    # --- egress proxy decisions (Fix-6 / append-only) ---
    EGRESS_ALLOWED = "egress_allowed"
    EGRESS_DENIED = "egress_denied"


class AuditChainCorrupted(RuntimeError):
    """Hash chain mismatch — tampering detectado."""


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Entrada firmada del log."""

    entry_id: UUID
    node_installation_id: UUID | None
    tenant_id: UUID | None
    timestamp: datetime
    actor: str
    audit_kind: AuditKind
    category: str | None
    description: str
    payload_hash_hex: str
    prev_entry_hash_hex: str
    signed_payload_hash_hex: str
    signature_hex: str


def _canonicalize(payload: dict[str, Any]) -> bytes:
    """JSON canónico determinista — orden alfabético + sin espacios."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class AuditHashChainSigner:
    """Firma una cadena de audit entries HMAC-SHA-256.

    Args:
        signing_key: secreto simétrico (en producción viene de la KMS;
            en personal-desktop reside cifrado con la passphrase LUKS).
        clock: callable opcional para timestamps deterministas en tests.
    """

    def __init__(
        self,
        *,
        signing_key: bytes,
        clock=lambda: datetime.now(tz=UTC),
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("signing_key debe tener al menos 32 bytes")
        self._key = signing_key
        self._clock = clock
        self._last_hash: bytes = _GENESIS_PREV_HASH
        # Serializes sign+persist across N concurrent asyncio workers (CTRL-P1-21).
        # The lock is owned by the signer instance (singleton shared by the pool).
        # Callers that need atomic sign+persist must hold this lock for the entire
        # critical section: sign → persist → release.  See append_and_persist().
        self._chain_lock: asyncio.Lock = asyncio.Lock()

    def append(
        self,
        *,
        audit_kind: AuditKind,
        actor: str,
        description: str,
        payload: dict[str, Any],
        node_installation_id: UUID | None = None,
        tenant_id: UUID | None = None,
        category: str | None = None,
    ) -> AuditEntry:
        """Firma una entrada y avanza _last_hash.

        NOTA: este método es síncrono y carece de await — es atómico bajo
        asyncio en un solo worker. Pero con N workers concurrentes el par
        (sign → await persist) tiene un hueco: otro worker puede firmar con
        un prev_hash correcto Y persistir antes, invirtiendo el orden en DB.
        Usa append_and_persist() para el caso N>1.
        """
        canonical = _canonicalize(payload)
        payload_hash = hashlib.sha256(canonical).digest()
        prev = self._last_hash
        signed_payload_hash = hashlib.sha256(payload_hash + prev).digest()
        signature = hmac.new(
            self._key, signed_payload_hash, hashlib.sha256
        ).digest()

        entry = AuditEntry(
            entry_id=uuid4(),
            node_installation_id=node_installation_id,
            tenant_id=tenant_id,
            timestamp=self._clock(),
            actor=actor,
            audit_kind=audit_kind,
            category=category,
            description=description,
            payload_hash_hex=payload_hash.hex(),
            prev_entry_hash_hex=prev.hex(),
            signed_payload_hash_hex=signed_payload_hash.hex(),
            signature_hex=signature.hex(),
        )
        self._last_hash = signed_payload_hash
        return entry

    async def append_and_persist(
        self,
        *,
        audit_kind: AuditKind,
        actor: str,
        description: str,
        payload: dict[str, Any],
        audit_repo: Any,
        node_installation_id: UUID | None = None,
        tenant_id: UUID | None = None,
        category: str | None = None,
    ) -> AuditEntry:
        """Firma Y persiste de forma atómica bajo _chain_lock.

        Este es el método correcto cuando hay N workers concurrentes (TASK 1).
        El lock serializa el par (sign → persist) de modo que el orden de
        signed_payload_hash en DB coincide con el orden de la cadena en memoria.

        Args:
            audit_repo: cualquier objeto con ``async def append(entry) -> None``.
                        Acepta Any para no crear acoplamiento de imports cruzados
                        entre application layers.
        """
        async with self._chain_lock:
            entry = self.append(
                audit_kind=audit_kind,
                actor=actor,
                description=description,
                payload=payload,
                node_installation_id=node_installation_id,
                tenant_id=tenant_id,
                category=category,
            )
            await audit_repo.append(entry)
        return entry

    def verify_chain(self, entries: list[AuditEntry]) -> None:
        """Valida la cadena completa — eleva AuditChainCorrupted si rompe.

        Args:
            entries: lista ordenada por timestamp ascendente.
        """
        prev = _GENESIS_PREV_HASH
        for entry in entries:
            if entry.prev_entry_hash_hex != prev.hex():
                raise AuditChainCorrupted(
                    f"prev_entry_hash mismatch en entry {entry.entry_id}: "
                    f"expected={prev.hex()[:16]}... "
                    f"got={entry.prev_entry_hash_hex[:16]}..."
                )
            recomputed = hashlib.sha256(
                bytes.fromhex(entry.payload_hash_hex) + prev
            ).digest()
            if recomputed.hex() != entry.signed_payload_hash_hex:
                raise AuditChainCorrupted(
                    f"signed_payload_hash mismatch en entry {entry.entry_id}"
                )
            expected_sig = hmac.new(
                self._key, recomputed, hashlib.sha256
            ).digest()
            if not hmac.compare_digest(expected_sig.hex(), entry.signature_hex):
                raise AuditChainCorrupted(
                    f"HMAC signature inválida en entry {entry.entry_id}"
                )
            prev = recomputed

    @property
    def head_hash_hex(self) -> str:
        """Hash de la última entrada firmada — para anclar a TSA externa."""
        return self._last_hash.hex()
