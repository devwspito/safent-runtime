"""SqliteAgentState — implementación de AgentStatePort sobre SQLite.

Singleton `agent_runtime_state` (creado por ensure_tasks_schema).
is_paused() lee la columna loop_state del singleton.
pause/resume persisten el cambio y emiten audit AGENT_PAUSED/AGENT_RESUMED
cuando signer+audit_repo están inyectados (US3/T047, CTRL-12).

Patrón: conexión por llamada, autocommit, row_factory sqlite3.Row.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from hermes.tasks.domain.ports import AgentStatePort
from hermes.tasks.infrastructure.schema import ensure_tasks_schema

if TYPE_CHECKING:
    from hermes.agents_os.application.audit_hash_chain import (
        AuditHashChainSigner,
    )
    from hermes.capabilities.domain.ports import SignedAuditRepositoryPort


class SqliteAgentState:
    """Estado de pausa/run del loop — backed by agent_runtime_state singleton.

    Args:
        db_path:    Ruta al shell-state.db.
        signer:     AuditHashChainSigner — firma cada transición
                    AGENT_PAUSED/AGENT_RESUMED (CTRL-12/AUD-1).
        audit_repo: SignedAuditRepositoryPort — persiste la entrada de audit
                    firmada.

    Raises:
        RuntimeError: si `signer` o `audit_repo` son None. Antes de este fix
            ambos eran opcionales y `_emit_audit_paused`/`_emit_audit_resumed`
            se limitaban a un `return` silencioso — construir el estado sin
            ellos dejaba el freno de emergencia (`safent brake release`,
            engage/release por API) auditado en NINGUNA parte: la cadena
            firmada crecía con otros eventos pero 0 `AGENT_PAUSED`/
            `AGENT_RESUMED` (CLI-N4, specs/025-safent-repaso
            matriz-final-39eeb8e, CWE-778). Ahora es fail-loud: sin
            signer+audit_repo reales, este objeto ni siquiera se construye.
            Tests que no necesiten verificar el audit chain deben pasar un
            signer/audit_repo de prueba real (ver tests/tasks/
            test_kill_switch.py) en vez de omitirlos.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        signer: AuditHashChainSigner,
        audit_repo: SignedAuditRepositoryPort,
    ) -> None:
        if signer is None or audit_repo is None:
            raise RuntimeError(
                "hermes.tasks.sqlite_agent_state.audit_wiring_missing: "
                "signer y audit_repo son obligatorios — AGENT_PAUSED/"
                "AGENT_RESUMED nunca deben auditarse en silencio "
                "(CTRL-12/CWE-778, CLI-N4)."
            )
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._signer = signer
        self._audit_repo = audit_repo
        self._ensure_schema()

    # ------------------------------------------------------------------
    # AgentStatePort
    # ------------------------------------------------------------------

    async def is_paused(self) -> bool:
        """True si loop_state == 'paused'."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT loop_state FROM agent_runtime_state WHERE id = 'singleton'"
            ).fetchone()
        if row is None:
            return False
        return row["loop_state"] == "paused"

    async def pause(
        self, *, by: UUID | None, reason: str, provenance: str = ""
    ) -> None:
        """Pausa el loop — persiste loop_state = 'paused', emite AGENT_PAUSED.

        `provenance` (AgentPauseProvenance, 025 re-verificación d2eb8c6) es
        audit-only, igual que el `reason` de provenance de `resume`: nunca se
        persiste en `agent_runtime_state` (esa columna es el `reason` humano,
        "por qué está pausado", que la UI muestra tal cual).
        """
        now_iso = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agent_runtime_state
                SET loop_state = 'paused',
                    reason     = ?,
                    changed_by = ?,
                    updated_at = ?
                WHERE id = 'singleton'
                """,
                (reason, str(by) if by else None, now_iso),
            )
        await self._emit_audit_paused(by=by, reason=reason, provenance=provenance)

    async def resume(self, *, by: UUID | None, reason: str = "") -> None:
        """Reanuda el loop — persiste loop_state = 'running', emite AGENT_RESUMED.

        `reason` is audit-only (AGENT_RESUMED payload/description) — the
        `agent_runtime_state.reason` COLUMN stays cleared to NULL on every
        resume regardless: that column means "why is it currently paused",
        not "what released it", and a stale pause-reason surviving a resume
        would misreport `status()` to any caller reading it while running.
        """
        now_iso = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agent_runtime_state
                SET loop_state = 'running',
                    reason     = NULL,
                    changed_by = ?,
                    updated_at = ?
                WHERE id = 'singleton'
                """,
                (str(by) if by else None, now_iso),
            )
        await self._emit_audit_resumed(by=by, reason=reason)

    async def status(self) -> dict:
        """Snapshot del freno: {engaged, reason, changed_by, changed_at}."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT loop_state, reason, changed_by, updated_at"
                " FROM agent_runtime_state WHERE id = 'singleton'"
            ).fetchone()
        if row is None:
            return {"engaged": False, "reason": None, "changed_by": None, "changed_at": None}
        return {
            "engaged": row["loop_state"] == "paused",
            "reason": row["reason"],
            "changed_by": row["changed_by"],
            "changed_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            ensure_tasks_schema(conn)

    async def _emit_audit_paused(
        self, *, by: UUID | None, reason: str, provenance: str = ""
    ) -> None:
        """Firma y persiste AGENT_PAUSED — signer/audit_repo son invariantes
        de construcción (__init__ falla si faltan), nunca None aquí.

        `provenance` (025 re-verificación d2eb8c6, "echar el freno no tiene
        vocabulario de procedencia"): marcador cerrado (AgentPauseProvenance)
        machine-readable, simétrico al de `_emit_audit_resumed` — nunca
        texto libre del llamante, a diferencia de `reason`.
        """
        from hermes.agents_os.application.audit_hash_chain import AuditKind  # noqa: PLC0415

        await self._signer.append_and_persist(
            audit_kind=AuditKind.AGENT_PAUSED,
            actor=str(by) if by else "system",
            description=f"Agent paused: {reason}",
            payload={
                "changed_by": str(by) if by else None,
                "reason": reason,
                "provenance": provenance or None,
            },
            audit_repo=self._audit_repo,
        )

    async def _emit_audit_resumed(self, *, by: UUID | None, reason: str = "") -> None:
        """Firma y persiste AGENT_RESUMED.

        `reason` (security review 2026-09-10, MEDIUM finding — CWE-778/
        STRIDE-R): before this, a release via `safent brake release` (host
        access, no MFA) produced the EXACT SAME entry as a TOTP-gated UI
        release — the signed chain could not tell them apart. Included in
        BOTH `description` (human-readable on a plain audit read) and
        `payload` (machine-readable for tooling) — empty string for the
        normal path, "host_cli" for the sovereign fallback.

        signer/audit_repo are invariants of construction (__init__ fails if
        missing) — never None here.
        """
        from hermes.agents_os.application.audit_hash_chain import AuditKind  # noqa: PLC0415

        await self._signer.append_and_persist(
            audit_kind=AuditKind.AGENT_RESUMED,
            actor=str(by) if by else "system",
            description=f"Agent resumed ({reason})" if reason else "Agent resumed",
            payload={"changed_by": str(by) if by else None, "reason": reason or None},
            audit_repo=self._audit_repo,
        )


# Satisface AgentStatePort structural check
assert isinstance(SqliteAgentState.__new__(SqliteAgentState), AgentStatePort)
