"""Esquema SQLite del BC `capabilities` — buzón durable de aprobaciones HIGH.

`ensure_capabilities_schema(conn)` aplica, de forma idempotente, el DDL de
`pending_approvals`: la mailbox que respalda `ApprovalGatePort` (contracts/
capabilities_ports.py). Una fila = una propuesta HIGH/untrusted pendiente de
resolución humana (threat-model CTRL-1).

El broker registra el HIGH pendiente sin UI nueva; el operador resuelve por la
API de supervisión EXISTENTE (NO dispara run_cycle — NFR-001). La aprobación
emite un approval_token VERIFICABLE (HMAC ligado a proposal_id, single-use):
`token_hmac` + `nonce` + `expires_at` + `consumed_at` lo respaldan.

Columnas alineadas con `ApprovalGatePort.register_pending` (proposal_id,
work_item_id, consent_context.{operator_id,tenant_id}, risk, justification,
parameters_redacted) y con `approve` (approved_by, SC-004). Idempotente por
`proposal_id` (PK) — un re-`register_pending` es `INSERT OR IGNORE`/upsert
controlado por el repo (T041).

Tipos coherentes con el resto del store: timestamps TEXT ISO-8601 UTC, UUIDs
TEXT, JSON TEXT.
"""

from __future__ import annotations

import sqlite3

_DDL_PENDING_APPROVALS = """
CREATE TABLE IF NOT EXISTS pending_approvals (
    -- Identidad: una fila por propuesta HIGH (idempotente por proposal_id).
    proposal_id          TEXT PRIMARY KEY,

    -- Tarea de la cola que originó la propuesta (re-encola al aprobar).
    work_item_id         TEXT NOT NULL,

    -- Consent context (operator_id None => fail-closed en el broker).
    tenant_id            TEXT,
    operator_id          TEXT NOT NULL,

    -- Clasificación de riesgo server-side (CapabilityRegistry).
    risk                 TEXT NOT NULL
        CHECK (risk IN ('low','high')),

    -- Nombre real del tool de la propuesta. CRÍTICO: la capa de MFA clasifica la
    -- delicadeza (TOTP / +humanidad / +acertijo) por el tool — si esto no se
    -- persiste+emite, la escalera MFA colapsa a TOTP plano (red-team 2026-06-19).
    tool_name            TEXT,

    -- Digest de la ACCIÓN exacta (sha256 de tool_name+args canónicos+task). Liga la
    -- aprobación a lo que el dueño vio: al reanudar, el chokepoint nativo solo ejecuta
    -- si el digest de los args a ejecutar coincide (anti aprobar-otra-acción).
    action_digest        TEXT,

    -- Por qué requiere HITL (para que el humano decida con contexto).
    justification        TEXT,

    -- Parámetros REDACTADOS (sin PII en claro — constitución III). JSON.
    parameters_redacted  TEXT NOT NULL DEFAULT '{}',

    -- Ciclo de vida de la aprobación.
    status               TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected')),

    -- Quién aprobó (autenticado, no del body del cliente — SC-004).
    approved_by          TEXT,

    -- Token de aprobación VERIFICABLE: HMAC ligado a (proposal,nonce,expiry),
    -- single-use. `consumed_at` marca el consumo (anti-replay).
    token_hmac           TEXT,
    nonce                TEXT,
    expires_at           TEXT,
    consumed_at          TEXT,

    -- Marcas temporales.
    created_at           TEXT NOT NULL,
    resolved_at          TEXT
);

-- Buscar aprobaciones por la tarea que las originó (re-encolar / observabilidad).
CREATE INDEX IF NOT EXISTS pending_approvals_work_item_idx
    ON pending_approvals (work_item_id);
"""


def ensure_capabilities_schema(conn: sqlite3.Connection) -> None:
    """Aplica el DDL de `pending_approvals` sobre una conexión abierta.

    Idempotente: `CREATE TABLE/INDEX IF NOT EXISTS`. Re-ejecutar no destruye
    datos ni lanza.
    """
    conn.executescript(_DDL_PENDING_APPROVALS)
    # Migración para DBs creadas antes de la columna tool_name (2026-06-19):
    # ADD COLUMN es idempotente vía try/except (sqlite no tiene IF NOT EXISTS
    # para columnas). Sin esto, las DBs existentes no clasifican el MFA por tool.
    try:
        conn.execute("ALTER TABLE pending_approvals ADD COLUMN tool_name TEXT")
    except sqlite3.OperationalError:
        pass  # la columna ya existe
    # Migración action_digest (native per-action approval, 2026-06-19).
    try:
        conn.execute("ALTER TABLE pending_approvals ADD COLUMN action_digest TEXT")
    except sqlite3.OperationalError:
        pass  # la columna ya existe
    # Índice para la consulta del chokepoint nativo: aprobado+no-consumido por digest.
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS pending_approvals_action_digest_idx "
            "ON pending_approvals (action_digest)"
        )
    except sqlite3.OperationalError:
        pass
    # Índice UNIQUE parcial: garantiza que solo puede existir UNA fila 'pending' por
    # action_digest. Segunda red de seguridad para deduplicar tarjetas de aprobación:
    # aunque proposal_id sea determinista (uuid5), una DB antigua con filas ya pendientes
    # queda también protegida. WHERE status='pending' limita la unicidad a pendientes.
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS pending_approvals_digest_pending_uidx "
            "ON pending_approvals(action_digest) WHERE status='pending'"
        )
    except sqlite3.OperationalError:
        pass
    # Migración conversation_id (C — anclar tarjeta al hilo del chat, 2026-06-23).
    # Guarda el id REAL de la conversación de chat (resuelto por el engine vía
    # conversation_task_registry) para que el FE ancle la tarjeta de aprobación al
    # hilo correcto. (Antes se guardaba el task_id aleatorio del ciclo → no casaba.)
    try:
        conn.execute("ALTER TABLE pending_approvals ADD COLUMN conversation_id TEXT")
    except sqlite3.OperationalError:
        pass
