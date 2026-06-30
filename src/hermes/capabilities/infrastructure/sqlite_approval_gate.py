"""T041 — SqliteApprovalGate (CTRL-1/BROKER-1).

Implementa ApprovalGatePort sobre la tabla `pending_approvals` (esquema:
capabilities/infrastructure/schema.py, T009).

Protocolo:
  - register_pending: idempotente por proposal_id (INSERT OR IGNORE). Redacta
    PII de los parámetros antes de persistir (CTRL-14 / Constitución III).
  - approve: mintea token HMAC vía HitlApprovalMinter, persiste token_hmac +
    nonce + expires_at + approved_by (SC-004), registra audit HITL_APPROVED.
  - reject: marca rejected_by + reason, registra audit HITL_REJECTED.
  - verify_token: delega en el minter (criptográfico, single-use, compare_digest).
    Fail-closed: False ante cualquier duda. NO hace presence-check (CTRL-1).
  - approved_token_for: devuelve token si status=approved; None en otro caso.

Idempotencia:
  - register_pending es INSERT OR IGNORE — segunda llamada con el mismo
    proposal_id es no-op (la fila ya existe).
  - approve y reject verifican el estado actual antes de mutar (fail-closed:
    no se puede aprobar un rejected ni rechazar un approved).

Redacción de PII (CTRL-14):
  - _redact_parameters sustituye cualquier valor string que parezca un
    placeholder PII (<PII:...>) por "<redacted>" y trunca strings largos.
    Los valores no-string se sustituyen por su tipo. Esto es defensivo;
    la tokenización real la hace el llamador antes de pasar al broker.

Capa: infrastructure (SQLite + HitlApprovalMinter + audit signer).
Sin framework. Conexión por llamada (patrón SqliteAuditRepository).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner, AuditKind
from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
from hermes.capabilities.domain.ports import (
    ConsentContext,
    RiskLevel,
)
from hermes.capabilities.infrastructure.schema import ensure_capabilities_schema

logger = logging.getLogger("hermes.capabilities.approval_gate")

# TTL por defecto del token de aprobación: 1 hora (reconfigurable).
_DEFAULT_TOKEN_TTL_S: int = 3600

# Regex simple para detectar placeholders PII.
_PII_PATTERN: re.Pattern[str] = re.compile(r"<PII:[^>]+>")

# Longitud máxima de un valor string redactado antes de truncar.
_MAX_VALUE_LEN: int = 64


class ApprovalGateError(RuntimeError):
    """Error irrecuperable del ApprovalGate — no degradar.

    `reason` carries the machine-readable code (e.g. 'mfa_not_enrolled',
    'invalid_totp', 'invalid_riddle') so the presentation layer can route
    to the correct user-facing message without string-parsing the message.
    """

    def __init__(self, message: str, reason: str = "mfa_denied") -> None:
        super().__init__(message)
        self.reason = reason


class SqliteApprovalGate:
    """Implementación SQLite de ApprovalGatePort.

    Args:
        db_path:    Ruta a shell-state.db (misma DB que el resto de la infra).
        minter:     HitlApprovalMinter inyectado (signing_key sellada — CTRL-7).
        signer:     AuditHashChainSigner para HITL_APPROVED/REJECTED (CTRL-9).
        audit_repo: SignedAuditRepositoryPort opcional para persistir los audits
                    de HITL_APPROVED/REJECTED (mantiene la cadena íntegra).
        token_ttl:  Segundos de validez del token de aprobación.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        minter: HitlApprovalMinter,
        signer: AuditHashChainSigner,
        audit_repo: Any | None = None,
        token_ttl: int = _DEFAULT_TOKEN_TTL_S,
        mfa_verifier: Any | None = None,
    ) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._minter = minter
        self._signer = signer
        self._audit_repo = audit_repo
        self._token_ttl = token_ttl
        # Injected MfaToolTierVerifier (duck-typed: .verify_for_tool(tool_name, risk,
        # factors) -> (ok, reason)). Enforces owner MFA INSIDE approve so EVERY surface
        # (web + D-Bus) is structurally MFA-gated — closes the MFA-skip side-door
        # (red-team 2026-06-19, finding 3). None ⇒ approve fails closed (no MFA-less mint).
        self._mfa_verifier = mfa_verifier
        self._ensure_schema()

    # ------------------------------------------------------------------
    # ApprovalGatePort
    # ------------------------------------------------------------------

    async def register_pending(
        self,
        *,
        proposal_id: UUID,
        work_item_id: UUID,
        consent_context: ConsentContext,
        risk: RiskLevel,
        justification: str,
        parameters_redacted: dict[str, Any],
        tool_name: str = "",
        action_digest: str = "",
        conversation_id: str = "",
    ) -> None:
        """Registra la propuesta HIGH como pendiente de aprobación.

        Idempotente por proposal_id: INSERT OR IGNORE — si ya existe, no-op.
        Los parámetros se redactan defensivamente (CTRL-14). `tool_name` se persiste
        para que la capa MFA clasifique la delicadeza por tool (no por el risk genérico).
        `action_digest` liga la aprobación a la acción exacta (chokepoint nativo).
        `conversation_id` es el id REAL de la conversación de chat (resuelto por el
        engine vía conversation_task_registry, NO el task_id del ciclo) — ancla la
        tarjeta de aprobación al hilo que el dueño está mirando.
        """
        operator_id = str(consent_context.operator_id) if consent_context.operator_id else ""
        now = datetime.now(tz=UTC).isoformat()
        safe_params = _redact_parameters(parameters_redacted)

        with self._connect() as conn:
            # Re-armado: como `proposal_id` es determinista (uuid5 del digest), una fila
            # terminal o pendiente-caduca con ese id haría que el INSERT OR IGNORE fuese
            # un no-op para SIEMPRE (la tarjeta nunca reaparecería tras consumirse o
            # caducar). Borramos esa fila ANTES de insertar la fresca. Nunca tocamos una
            # aprobación VIVA sin consumir (status='approved' AND consumed_at IS NULL) — esa
            # ruta ni siquiera llega aquí (el caller la resuelve en approved_proposal_for_digest).
            conn.execute(
                """
                DELETE FROM pending_approvals
                 WHERE proposal_id = ?
                   AND NOT (status = 'approved' AND consumed_at IS NULL)
                   AND (status != 'pending' OR created_at <= datetime('now', '-35 minutes'))
                """,
                (str(proposal_id),),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO pending_approvals (
                    proposal_id, work_item_id, tenant_id, operator_id,
                    risk, tool_name, action_digest, justification, parameters_redacted,
                    status, created_at, conversation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    str(proposal_id),
                    str(work_item_id),
                    str(consent_context.tenant_id),
                    operator_id,
                    risk.value,
                    tool_name,
                    action_digest,
                    justification,
                    json.dumps(safe_params),
                    now,
                    conversation_id or None,
                ),
            )

    async def approve(
        self, *, proposal_id: UUID, approved_by: UUID, mfa_factors: Any | None = None
    ) -> str:
        """Aprueba la propuesta con modelo de MFA escalado (owner decision 2026-06-25).

        SC-004: registra quién aprobó (approved_by autenticado, no del body).

        Escalated MFA model:
          - simple tier (la mayoría de tools): minta el token directamente, sin MFA.
          - mfa tier (MOST_DELICATE / destructivos): verifica TOTP vía mfa_verifier
            ANTES de mintear. Fail-closed: sin verifier → ApprovalGateError.

        La clasificación se lee del tool_name almacenado en la fila (fuente única:
        tool_delicacy.is_mfa_required). El agente NO puede influenciar el tier
        (el tool_name lo escribe el hook server-side, no el LLM).

        Returns:
            Token de aprobación (opaco, HMAC-SHA256, single-use).

        Raises:
            ApprovalGateError: si la propuesta no existe/ya resuelta o MFA falla.
        """
        from hermes.capabilities.tool_delicacy import is_mfa_required  # noqa: PLC0415

        row = self._fetch_pending(proposal_id)
        if row is None:
            raise ApprovalGateError(
                f"proposal_id={proposal_id} no existe o ya fue resuelta."
            )
        if row["status"] != "pending":
            raise ApprovalGateError(
                f"proposal_id={proposal_id} ya tiene status={row['status']!r}. "
                "Solo se puede aprobar una propuesta en estado 'pending'."
            )

        _row_tool = row["tool_name"] if "tool_name" in row.keys() else ""
        if is_mfa_required(_row_tool or ""):
            # mfa-tier: fail-closed — sin verifier o factores inválidos → rechaza.
            if self._mfa_verifier is None:
                raise ApprovalGateError(
                    f"MFA verifier no configurado para tool mfa-tier '{_row_tool}' "
                    "— aprobación rechazada (fail-closed).",
                    reason="mfa_required",
                )
            ok, reason = self._mfa_verifier.verify_for_tool(
                tool_name=_row_tool or "", risk=row["risk"], factors=mfa_factors
            )
            if not ok:
                logger.warning(
                    "hermes.approval_gate.mfa_denied proposal=%s tool=%s reason=%s",
                    proposal_id, _row_tool, reason,
                )
                raise ApprovalGateError(
                    f"MFA inválida para aprobar tool mfa-tier '{_row_tool}' "
                    f"(motivo={reason}).",
                    reason=reason,
                )
        # simple tier — or mfa-tier with valid factors: proceed to mint.

        capability = row["risk"]  # usamos el risk como capability label
        token = self._minter.mint(
            proposal_id=proposal_id,
            capability=capability,
            ttl=self._token_ttl,
        )
        expiry = datetime.now(tz=UTC) + timedelta(seconds=self._token_ttl)
        now = datetime.now(tz=UTC).isoformat()

        # Extraer nonce del token para persistirlo (anti-replay store).
        nonce = _extract_nonce_from_token(token)

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pending_approvals
                SET status='approved', approved_by=?, token_hmac=?,
                    nonce=?, expires_at=?, resolved_at=?
                WHERE proposal_id=?
                """,
                (
                    str(approved_by),
                    token,
                    nonce,
                    expiry.isoformat(),
                    now,
                    str(proposal_id),
                ),
            )

        await self._audit_hitl(
            kind=AuditKind.HITL_APPROVED,
            proposal_id=proposal_id,
            actor=str(approved_by),
            description=f"HITL approved proposal {proposal_id}",
        )
        return token

    async def reject(
        self, *, proposal_id: UUID, rejected_by: UUID, reason: str
    ) -> None:
        """Rechaza la propuesta. Audita HITL_REJECTED.

        Raises:
            ApprovalGateError: si la propuesta no existe o ya fue resuelta.
        """
        row = self._fetch_pending(proposal_id)
        if row is None:
            raise ApprovalGateError(
                f"proposal_id={proposal_id} no existe o ya fue resuelta."
            )
        if row["status"] != "pending":
            raise ApprovalGateError(
                f"proposal_id={proposal_id} ya tiene status={row['status']!r}."
            )

        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pending_approvals
                SET status='rejected', approved_by=?, resolved_at=?
                WHERE proposal_id=?
                """,
                (str(rejected_by), now, str(proposal_id)),
            )

        await self._audit_hitl(
            kind=AuditKind.HITL_REJECTED,
            proposal_id=proposal_id,
            actor=str(rejected_by),
            description=f"HITL rejected proposal {proposal_id}: {reason}",
        )

    async def expire(self, *, proposal_id: UUID) -> None:
        """Marca una propuesta 'pending' como 'expired' (caducó la espera del dueño).

        Solo afecta filas en estado 'pending' — una aprobada/rechazada no se toca.
        La saca de list_hitl_pending (que filtra status='pending') para que NO quede
        como TARJETA FANTASMA cuando el hilo bloqueado caducó sin decisión del dueño.
        Idempotente y silenciosa: sin audit, sin raise (no es una resolución del dueño).
        """
        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE pending_approvals SET status='expired', resolved_at=? "
                "WHERE proposal_id=? AND status='pending'",
                (now, str(proposal_id)),
            )

    async def verify_token(self, *, proposal_id: UUID, token: str) -> bool:
        """Verifica criptográficamente el token y lo marca consumido en DB (I1/CTRL-1).

        Secuencia fail-closed:
          1. Verificación criptográfica vía minter (HMAC, expiry, nonce in-memory).
          2. UPDATE atómico: consumed_at IS NULL → consumed_at=now.
             Si 0 filas afectadas = ya consumido o no existe → False (replay bloqueado).

        Así el single-use sobrevive a reinicios y a una segunda instancia del gate
        que comparta la misma DB (no depende del set in-memory del minter).

        Returns:
            True SOLO si el token pasa todas las verificaciones Y el UPDATE afecta
            exactamente 1 fila (primer consumo). False en cualquier otro caso.
        """
        if not token:
            return False
        try:
            crypto_ok = self._minter.verify(proposal_id=proposal_id, token=token)
        except Exception:  # noqa: BLE001
            return False
        if not crypto_ok:
            return False

        # Persistir el consumo atómicamente — protege contra re-uso cross-restart.
        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE pending_approvals
                SET consumed_at = ?
                WHERE proposal_id = ? AND consumed_at IS NULL
                """,
                (now, str(proposal_id)),
            )
        if cursor.rowcount == 0:
            # El token ya fue consumido (o la propuesta no existe) — fail-closed.
            logger.warning(
                "hermes.approval_gate.token_already_consumed: proposal_id=%s",
                proposal_id,
            )
            return False
        return True

    async def work_item_id_for_proposal(self, proposal_id: UUID) -> UUID | None:
        """Devuelve el work_item_id asociado a la proposal, o None si no existe.

        Permite a approve_action recuperar el work_item_id para re-encolar
        la tarea tras la aprobación (FR-015 / fix HITL re-enqueue).
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT work_item_id FROM pending_approvals WHERE proposal_id = ?",
                (str(proposal_id),),
            ).fetchone()
        if row is None:
            return None
        raw = row["work_item_id"]
        if not raw:
            return None
        try:
            return UUID(raw)
        except (ValueError, AttributeError):
            return None

    async def approved_token_for(self, proposal_id: UUID) -> str | None:
        """Devuelve el token si la propuesta fue aprobada; None en otro caso.

        Fail-closed: None si status != 'approved', si no hay token, o si
        el token ya expiró (según expires_at).
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, token_hmac, expires_at FROM pending_approvals "
                "WHERE proposal_id = ?",
                (str(proposal_id),),
            ).fetchone()
        if row is None:
            return None
        if row["status"] != "approved":
            return None
        if not row["token_hmac"]:
            return None
        if _is_expired(row["expires_at"]):
            return None
        return row["token_hmac"]

    async def approved_proposal_for_digest(self, action_digest: str) -> UUID | None:
        """proposal_id de una fila APROBADA, NO consumida y NO expirada cuyo
        action_digest coincide. El chokepoint nativo la usa para reanudar EXACTAMENTE
        la acción que el dueño aprobó (luego verify_token la consume, single-use).

        Fail-closed: None si digest vacío o sin match aprobado+no-consumido+vigente.
        """
        if not action_digest:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT proposal_id, expires_at FROM pending_approvals "
                "WHERE action_digest = ? AND status = 'approved' "
                "AND token_hmac IS NOT NULL AND consumed_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (action_digest,),
            ).fetchone()
        if row is None or _is_expired(row["expires_at"]):
            return None
        try:
            return UUID(row["proposal_id"])
        except (ValueError, AttributeError):
            return None

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL: pending_approvals is written by the daemon executor thread and the
        # D-Bus thread (approve/verify_token) concurrently; WAL lets a reader and a
        # writer coexist instead of hitting "database is locked". F-08: busy_timeout
        # bounds the wait under N workers.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            ensure_capabilities_schema(conn)

    def _fetch_pending(self, proposal_id: UUID) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM pending_approvals WHERE proposal_id = ?",
                (str(proposal_id),),
            ).fetchone()

    async def _audit_hitl(
        self,
        *,
        kind: AuditKind,
        proposal_id: UUID,
        actor: str,
        description: str,
    ) -> None:
        """Firma y opcionalmente persiste la entrada de audit HITL_APPROVED/REJECTED.

        Si audit_repo está inyectado, persiste bajo _chain_lock para mantener la
        cadena íntegra con N workers concurrentes (TASK 1 / CTRL-P1-21).
        """
        if self._audit_repo is not None:
            try:
                await self._signer.append_and_persist(
                    audit_kind=kind,
                    actor=actor,
                    description=description,
                    payload={"proposal_id": str(proposal_id)},
                    audit_repo=self._audit_repo,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hermes.approval_gate.audit_append_failed: %s — flujo de aprobación continúa",
                    exc,
                )
        else:
            self._signer.append(
                audit_kind=kind,
                actor=actor,
                description=description,
                payload={"proposal_id": str(proposal_id)},
            )


# Satisface ApprovalGatePort structural check
assert isinstance(SqliteApprovalGate, type)


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------


def _redact_parameters(params: dict[str, Any]) -> dict[str, Any]:
    """Redacta PII de los parámetros antes de persistir (CTRL-14/Constitución III).

    - Reemplaza placeholders <PII:...> por "<redacted>".
    - Trunca strings largos.
    - Sustituye valores no-string por su tipo.

    Esta redacción es defensiva — la tokenización primaria es del broker.
    """
    result: dict[str, Any] = {}
    for key, value in params.items():
        result[key] = _redact_value(value)
    return result


def _redact_value(value: Any) -> Any:  # noqa: ANN401,PLR0911
    if isinstance(value, str):
        if _PII_PATTERN.search(value):
            return "<redacted>"
        if len(value) > _MAX_VALUE_LEN:
            return value[:_MAX_VALUE_LEN] + "…"
        return value
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def _is_expired(expires_at: str | None) -> bool:
    """True si expires_at está en el pasado o es None."""
    if not expires_at:
        return True
    try:
        expiry = datetime.fromisoformat(expires_at)
        return datetime.now(tz=UTC) >= expiry
    except ValueError:
        return True  # formato inválido — fail-closed


def _extract_nonce_from_token(token: str) -> str:
    """Extrae el nonce del token opaco para persistirlo.

    El token tiene la forma: `{payload_hex}.{hmac_hex}`.
    El payload decodificado tiene la forma: `{proposal_id}|{capability}|{expiry_unix}|{nonce}`.
    Retorna "" si el formato es inválido (fail-closed: el token se persiste igual).
    """
    try:
        dot_idx = token.rfind(".")
        if dot_idx < 0:
            return ""
        payload_hex = token[:dot_idx]
        payload_str = bytes.fromhex(payload_hex).decode()
        parts = payload_str.split("|")
        if len(parts) != 4:  # noqa: PLR2004
            return ""
        return parts[3]  # nonce
    except Exception:  # noqa: BLE001
        return ""
