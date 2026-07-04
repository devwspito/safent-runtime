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

# Durable breaker (2026-07): tope de re-registros (attempt_count) de la MISMA
# propuesta (mismo proposal_id determinista) mientras sigue 'pending' antes de
# rechazarla terminalmente. Generoso a propósito — una fila legítima se
# re-registra ~2 veces por ciclo (dispatch in-cycle del engine + dispatch de
# salida del orchestrator), así que un umbral bajo cortaría una aprobación
# lenta pero legítima tras un puñado de re-encolados/re-reclamados (lease
# churn). Mirror del thread-local _MAX_WRITE_TOOL_FAILURES de nous_engine, pero
# durable — ese contador se resetea en cada ciclo/hilo nuevo tras un
# re-enqueue, así que no detiene un loop que cruza varios re-encolados.
_MAX_DURABLE_PENDING_ATTEMPTS: int = 20


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
        route: str = "",
        sensitivity_categories: frozenset[str] = frozenset(),
        agent_id: str = "",
    ) -> str:
        """Registra la propuesta HIGH como pendiente de aprobación.

        Idempotente por proposal_id: INSERT OR IGNORE — si ya existe, no-op de
        inserción; si además sigue 'pending', cuenta el re-registro vía
        attempt_count (durable breaker, ver abajo). Los parámetros se redactan
        defensivamente (CTRL-14). `tool_name` se persiste
        para que la capa MFA clasifique la delicadeza por tool (no por el risk
        genérico). `action_digest` liga la aprobación a la acción exacta
        (chokepoint nativo). `conversation_id` es el id REAL de la conversación
        de chat (resuelto por el engine vía conversation_task_registry, NO el
        task_id del ciclo) — ancla la tarjeta de aprobación al hilo que el dueño
        está mirando.

        `route`/`sensitivity_categories`/`agent_id` (Fase 2 Phase 4b): cuando
        `route == "enterprise"` la fila queda enrutada a un aprobador remoto de
        Enterprise — `approve()` rechaza (fail-closed) cualquier intento LOCAL
        de aprobarla; `sensitivity_categories`/`agent_id` viajan como contexto
        para el push loop de `hermes.config_sync.remote_approvals`. `route=""`
        (default) es LOCAL — comportamiento de hoy, sin cambio.

        Returns:
            El status resultante de la fila: 'pending' en el caso normal, o
            'rejected' si el durable breaker (attempt_count >
            _MAX_DURABLE_PENDING_ATTEMPTS) la rechazó terminalmente — el broker
            debe traducir esto a REJECTED_BY_POLICY en vez de PENDING_APPROVAL
            (fail-closed: deja de re-aparecer como tarjeta).
        """
        operator_id = str(consent_context.operator_id) if consent_context.operator_id else ""
        now = datetime.now(tz=UTC).isoformat()
        safe_params = _redact_parameters(parameters_redacted)
        zero_work_item = str(UUID(int=0))
        work_item_id_str = str(work_item_id)
        sensitivity_json = (
            json.dumps(sorted(str(c) for c in sensitivity_categories))
            if route == "enterprise" and sensitivity_categories
            else None
        )

        with self._connect() as conn:
            # Re-armado: como `proposal_id` es determinista (uuid5 del digest), una fila
            # terminal o pendiente-caduca con ese id haría que el INSERT fuese un no-op
            # para SIEMPRE (la tarjeta nunca reaparecería tras consumirse o caducar).
            # Borramos esa fila ANTES de insertar la fresca. Nunca tocamos una
            # aprobación VIVA sin consumir (status='approved' AND consumed_at IS NULL) — esa
            # ruta ni siquiera llega aquí (el caller la resuelve en approved_proposal_for_digest).
            # attempt_count > threshold excluded from revival: a row the durable
            # breaker rejected must STAY rejected for this exact proposal_id (the
            # whole point of the breaker) — without this exclusion, the very next
            # register_pending call would delete-and-recreate it fresh (attempt_count
            # reset to 1) since 'rejected' also satisfies `status != 'pending'`,
            # making the breaker a periodic blip instead of a durable stop. A
            # genuine human reject/expire (attempt_count under the threshold) is
            # still revived exactly as before — only breaker-tripped rows are exempt.
            conn.execute(
                """
                DELETE FROM pending_approvals
                 WHERE proposal_id = ?
                   AND NOT (status = 'approved' AND consumed_at IS NULL)
                   AND attempt_count <= ?
                   AND (status != 'pending' OR created_at <= datetime('now', '-35 minutes'))
                """,
                (str(proposal_id), _MAX_DURABLE_PENDING_ATTEMPTS),
            )
            # Idempotente por proposal_id: INSERT OR IGNORE (preserva EXACTAMENTE
            # el comportamiento previo, incluida la protección de la UNIQUE parcial
            # sobre action_digest — un ON CONFLICT(proposal_id) explícito NO cubre
            # esa segunda constraint y rompía con IntegrityError en colisiones de
            # digest legítimas). rowcount distingue INSERT real (1) de no-op por
            # conflicto (0) — solo en el no-op incrementamos attempt_count abajo,
            # así una fila recién creada no se cuenta dos veces como "reintento".
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO pending_approvals (
                    proposal_id, work_item_id, tenant_id, operator_id,
                    risk, tool_name, action_digest, justification, parameters_redacted,
                    status, created_at, conversation_id, attempt_count,
                    route, sensitivity, agent_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, 1, ?, ?, ?)
                """,
                (
                    str(proposal_id),
                    work_item_id_str,
                    str(consent_context.tenant_id),
                    operator_id,
                    risk.value,
                    tool_name,
                    # SECURITY (2026-07, review Info): store NULL (not "") when there
                    # is no per-action digest. The broker/MCP path never sets one, so
                    # every broker pending row would otherwise share action_digest=""
                    # and collide on the partial UNIQUE index
                    # (pending_approvals_digest_pending_uidx WHERE status='pending') —
                    # the 2nd concurrent pending proposal's INSERT OR IGNORE is
                    # silently dropped → no row → an unapprovable phantom (exactly what
                    # happens when the CEO delegates several tool-gated tasks at once).
                    # SQLite treats NULLs as DISTINCT in a UNIQUE index, so broker rows
                    # dedup purely by proposal_id (PK); the native path keeps its real
                    # digest and still dedups identical actions.
                    action_digest or None,
                    justification,
                    json.dumps(safe_params),
                    now,
                    conversation_id or None,
                    route or None,
                    sensitivity_json,
                    agent_id or None,
                ),
            )
            if cursor.rowcount == 0:
                # La fila ya existía (proposal_id determinista) — durable breaker
                # (2026-07): cuenta el re-registro. No-op si ya no está 'pending'
                # (approved/rejected/expired no se tocan).
                conn.execute(
                    """
                    UPDATE pending_approvals
                       SET attempt_count = attempt_count + 1
                     WHERE proposal_id = ?
                       AND status = 'pending'
                    """,
                    (str(proposal_id),),
                )
            # BUG FIX (2026-07): heal a stale work_item_id=0 row. Before the
            # in-cycle WRITE dispatch threaded the real work_item_id through, the
            # FIRST register_pending call for a delegated/autonomous proposal
            # always persisted UUID(int=0) — and because proposal_id is
            # deterministic, every later call (carrying the correct id) was a
            # no-op, so the row stayed poisoned at 0 forever and approve_action
            # could never re-enqueue the task. Once a real id arrives, adopt it —
            # never touches a non-pending row or a row that already has a real id.
            if work_item_id_str != zero_work_item:
                # SECURITY (2026-07, review Medium/CWE-863): scope the heal to the
                # SAME tenant. proposal_id is deterministic over (tool, params) and
                # excludes tenant_id, so two tenants proposing a byte-identical action
                # share a row; without this guard a later tenant's cycle could rebind
                # the pending row to its own work_item_id → cross-tenant approval
                # confusion. Only ever moves zero→real on a pending row of THIS tenant.
                conn.execute(
                    """
                    UPDATE pending_approvals
                       SET work_item_id = ?
                     WHERE proposal_id = ?
                       AND work_item_id = ?
                       AND status = 'pending'
                       AND tenant_id = ?
                    """,
                    (
                        work_item_id_str,
                        str(proposal_id),
                        zero_work_item,
                        str(consent_context.tenant_id),
                    ),
                )

            row = conn.execute(
                "SELECT status, attempt_count FROM pending_approvals WHERE proposal_id = ?",
                (str(proposal_id),),
            ).fetchone()

        if row is None:  # pragma: no cover — defensive; the INSERT above guarantees a row
            return "pending"

        status = str(row["status"])
        attempt_count = row["attempt_count"] if row["attempt_count"] is not None else 1
        if status == "pending" and attempt_count > _MAX_DURABLE_PENDING_ATTEMPTS:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE pending_approvals SET status='rejected', resolved_at=? "
                    "WHERE proposal_id=? AND status='pending'",
                    (datetime.now(tz=UTC).isoformat(), str(proposal_id)),
                )
            logger.warning(
                "hermes.approval_gate.durable_breaker_tripped: proposal=%s tool=%s "
                "attempts=%d > %d — rechazo terminal, deja de re-aparecer como tarjeta.",
                proposal_id, tool_name, attempt_count, _MAX_DURABLE_PENDING_ATTEMPTS,
            )
            return "rejected"
        return status

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

        # I-1/I-3 (Fase 2 Phase 4b): una fila enrutada a Enterprise SOLO la puede
        # aprobar una decisión firmada de la nube (hermes.config_sync.
        # remote_approvals, vía signal_native_danger_approval) — el dueño LOCAL
        # nunca la mintea directamente, ni siquiera con MFA válida. Fail-closed:
        # el caller (D-Bus approve_action / approvals_api) debe surfacear esto
        # como "pendiente de aprobación de tu empresa", NO como un approve normal.
        # reject() (denegar) NO tiene este guard — el dueño local SIEMPRE puede
        # denegar (invariante I-2).
        _row_route = row["route"] if "route" in row.keys() else ""
        if _row_route == "enterprise":
            raise ApprovalGateError(
                f"proposal_id={proposal_id} está enrutada a aprobación de "
                "Enterprise — solo una decisión firmada de tu empresa puede "
                "aprobarla; el dueño local puede denegarla pero no aprobarla "
                "directamente.",
                reason="enterprise_route_requires_cloud_decision",
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
            cursor = conn.execute(
                """
                UPDATE pending_approvals
                SET status='approved', approved_by=?, token_hmac=?,
                    nonce=?, expires_at=?, resolved_at=?
                WHERE proposal_id=?
                  AND status='pending'
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
            if cursor.rowcount == 0:
                # TOCTOU (2026-07, review Info/CWE-367): the row stopped being
                # 'pending' between the _fetch_pending read above and this UPDATE —
                # e.g. the durable breaker flipped it to 'rejected' concurrently.
                # Do NOT clobber a terminal state back to 'approved'; the minted
                # token is never persisted (token_hmac stays unset) so it can't be
                # verified. Deterministic terminal state wins; fail as "already
                # resolved" (fail-closed).
                raise ApprovalGateError(
                    f"proposal_id={proposal_id} dejó de estar 'pending' "
                    "(resuelta en paralelo) — aprobación abortada."
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
