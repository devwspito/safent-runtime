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

──────────────────────────────────────────────────────────────────────────────
Migración status→'expired' (2026-07) — cierra el loop de tarjetas fantasma:

  `expire()` del gate escribe status='expired' cuando la espera del dueño caduca
  (para sacar la fila de list_hitl_pending, que filtra status='pending'). El
  CHECK original SOLO admitía ('pending','approved','rejected'), así que cada
  expire() lanzaba sqlite3.IntegrityError — tragado por el caller (except: pass).
  Efecto: la fila caducada NUNCA cambiaba de estado, se quedaba 'pending' y
  RE-APARECÍA como tarjeta fantasma. Este es el último eslabón del fix del loop
  de tarjetas (P0). El durable breaker (register_pending) usa 'rejected', que YA
  estaba en el CHECK — no lo rompía.

  SQLite no permite ALTER ... DROP/MODIFY CONSTRAINT: para ampliar el CHECK se
  recrea la tabla con el patrón oficial (CREATE new → INSERT SELECT → DROP old →
  RENAME) dentro de UNA transacción, preservando TODAS las filas. Guard de
  idempotencia = INSPECCIÓN DEL PROPIO CHECK en sqlite_master (si ya contiene
  'expired', no-op). NO se usa `PRAGMA user_version`: shell-state.db lo COMPARTE
  con el esquema `tasks`, que ya lo posee (valores 1→4) — un guard por
  user_version colisionaría con esa numeración.

──────────────────────────────────────────────────────────────────────────────
Columnas route/sensitivity/agent_id (Fase 2 Phase 4b) — Enterprise remote
approval (runtime/associate side):

  `route` persiste el veredicto de `capabilities.approval_router.route()` para
  ESTA fila: 'enterprise' cuando el dueño-empresa (no el dueño local) resuelve
  la aprobación; NULL/'' para LOCAL (comportamiento de hoy, sin cambio — el
  99% de las filas nunca escriben esta columna). NUNCA 'hardblock' — route()
  no lo produce jamás (ver approval_router.py). Fuentes de verdad:
    - `sqlite_approval_gate.approve()` rechaza (fail-closed, ApprovalGateError
      reason='enterprise_route_requires_cloud_decision') un intento de
      aprobación LOCAL sobre una fila route='enterprise' — SOLO una decisión
      firmada de la nube (hermes.config_sync.remote_approvals) puede aprobarla.
      `reject()` (denegar) NUNCA se gatea por route — el dueño local SIEMPRE
      puede denegar (invariante I-2).
    - `hermes.config_sync.remote_approvals` empuja a la nube únicamente las
      filas `route='enterprise'` AND `status='pending'`.

  `sensitivity` es el JSON de la lista de `SensitivityCategory` (pii_read/
  new_egress/spend) de ESTA acción — contexto informativo para el aprobador
  remoto (NO decide la ruta desde Fase 2 Phase 4c: la ruta depende solo de
  `tool_delicacy.is_mfa_required(tool)`); solo se persiste junto a
  route='enterprise'.

  `agent_id` es el agente del roster (ciclo ambiente) que propuso la acción —
  necesario para el body PINNED que el push loop de remote_approvals envía a
  la nube (el hilo que registra la fila SÍ tiene el agente ambiente; el push
  loop, en un hilo/tick posterior, no).

  Todas nullable, EXPAND puro vía ALTER (sin CHECK — el valor lo controla
  exclusivamente el código server-side, nunca el LLM ni el cliente HTTP).
"""

from __future__ import annotations

import sqlite3

# DDL de CREACIÓN sobre una DB virgen. El CHECK de `status` YA admite 'expired'
# (así una DB nueva nace correcta y la migración de abajo es no-op para ella).
# Las columnas conversation_id/attempt_count NO se declaran aquí: llegan por
# ALTER (histórico) — `_add_pending_approvals_columns` las añade idempotentemente.
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

    -- Ciclo de vida de la aprobación. 'expired' lo escribe expire() cuando la
    -- espera del dueño caduca sin decisión — la saca de list_hitl_pending para
    -- que no quede como tarjeta fantasma. Sin 'expired' en el CHECK, expire()
    -- lanzaba IntegrityError y la fila se quedaba 'pending' para siempre.
    status               TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected','expired')),

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
"""

# Índices de `pending_approvals`. Idempotentes (IF NOT EXISTS). Se (re)crean al
# FINAL de ensure_*: la recreación de la tabla (migración de status) hace DROP
# TABLE, que borra sus índices — este bloque los repuebla siempre.
_DDL_PENDING_APPROVALS_INDEXES = """
-- Buscar aprobaciones por la tarea que las originó (re-encolar / observabilidad).
CREATE INDEX IF NOT EXISTS pending_approvals_work_item_idx
    ON pending_approvals (work_item_id);

-- Chokepoint nativo: aprobado+no-consumido por digest.
CREATE INDEX IF NOT EXISTS pending_approvals_action_digest_idx
    ON pending_approvals (action_digest);

-- UNIQUE parcial: garantiza que solo exista UNA fila 'pending' por action_digest.
-- Segunda red para deduplicar tarjetas de aprobación (proposal_id ya es uuid5
-- determinista; esto blinda además DBs antiguas con pendientes preexistentes).
CREATE UNIQUE INDEX IF NOT EXISTS pending_approvals_digest_pending_uidx
    ON pending_approvals(action_digest) WHERE status='pending';
"""

# ── ALTER ADD COLUMN idempotentes (EXPAND histórico) ────────────────────────
# tool_name/action_digest ya están en el CREATE de arriba (DBs nuevas), pero un
# ALTER en try/except es no-op idempotente si la columna ya existe; conservado
# por si una DB muy antigua nació sin ellas. conversation_id/attempt_count SOLO
# existen vía ALTER. Debe correr ANTES de la recreación de status: garantiza que
# la tabla vieja tiene TODAS las columnas que el INSERT SELECT va a copiar.
_PENDING_APPROVALS_ADD_COLUMNS: tuple[str, ...] = (
    # tool_name (2026-06-19): clasifica el tier MFA por tool.
    "ALTER TABLE pending_approvals ADD COLUMN tool_name TEXT",
    # action_digest (2026-06-19): native per-action approval.
    "ALTER TABLE pending_approvals ADD COLUMN action_digest TEXT",
    # conversation_id (2026-06-23): ancla la tarjeta al hilo del chat.
    "ALTER TABLE pending_approvals ADD COLUMN conversation_id TEXT",
    # attempt_count (2026-07): durable breaker anti-loop de tarjetas.
    "ALTER TABLE pending_approvals ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1",
    # route (Fase 2 Phase 4b): 'enterprise' cuando approval_router.route() enrutó
    # ESTA fila a un aprobador remoto de Enterprise; NULL/'' = LOCAL (hoy, sin
    # cambio). Nunca 'hardblock' (approval_router.route() nunca lo produce — ver
    # capabilities/approval_router.py). Fuente única para: (a) el push loop de
    # remote_approvals (solo empuja filas route='enterprise'), (b) el gate
    # rechazando un approve LOCAL sobre una fila enrutada a Enterprise (solo una
    # decisión firmada de la nube puede aprobarla — I-1/I-3).
    "ALTER TABLE pending_approvals ADD COLUMN route TEXT",
    # sensitivity (Fase 2 Phase 4b): JSON de la lista de SensitivityCategory (p.ej.
    # '["pii_read"]') que aportó la elegibilidad ENTERPRISE de esta fila. Solo se
    # persiste cuando route='enterprise' (contexto para el aprobador remoto);
    # NULL para filas LOCAL (comportamiento de hoy sin cambio).
    "ALTER TABLE pending_approvals ADD COLUMN sensitivity TEXT",
    # agent_id (Fase 2 Phase 4b): agente del roster que propuso la acción (ciclo
    # ambiente, conversation_task_registry.get_current_cycle_agent()). Necesario
    # para el body PINNED que remote_approvals empuja a la nube ({..., "agent_id",
    # ...}) — sin persistirlo en el registro no sobrevive al push loop asíncrono
    # (que corre en un hilo/tick distinto al que bloqueó la conversación).
    "ALTER TABLE pending_approvals ADD COLUMN agent_id TEXT",
)

# 🔒 RECREACIÓN — `pending_approvals` con el CHECK de `status` ampliado a
# 'expired'. Mismas columnas que la tabla viva tras el EXPAND (incluidas
# conversation_id/attempt_count, aquí declaradas de primera clase). El resto de
# CHECK (risk) se replica TEXTUALMENTE. UN solo statement CREATE TABLE
# (se ejecuta con `execute`, no `executescript`, para no romper la transacción).
_DDL_PENDING_APPROVALS_NEW = """
CREATE TABLE pending_approvals_new (
    proposal_id          TEXT PRIMARY KEY,
    work_item_id         TEXT NOT NULL,
    tenant_id            TEXT,
    operator_id          TEXT NOT NULL,
    risk                 TEXT NOT NULL
        CHECK (risk IN ('low','high')),
    tool_name            TEXT,
    action_digest        TEXT,
    justification        TEXT,
    parameters_redacted  TEXT NOT NULL DEFAULT '{}',
    status               TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected','expired')),
    approved_by          TEXT,
    token_hmac           TEXT,
    nonce                TEXT,
    expires_at           TEXT,
    consumed_at          TEXT,
    created_at           TEXT NOT NULL,
    resolved_at          TEXT,
    conversation_id      TEXT,
    attempt_count        INTEGER NOT NULL DEFAULT 1,
    route                TEXT,
    sensitivity          TEXT,
    agent_id             TEXT
);
"""

# Copia explícita POR NOMBRE: el orden físico de columnas difiere entre DBs
# viejas (tool_name/action_digest/conversation_id/attempt_count/route/
# sensitivity/agent_id llegan por ALTER, al final) y nuevas (nacen en el
# CREATE) — el SELECT nombrado mapea correcto sin depender de posiciones.
_PENDING_APPROVALS_COLUMNS = (
    "proposal_id, work_item_id, tenant_id, operator_id, risk, tool_name, "
    "action_digest, justification, parameters_redacted, status, approved_by, "
    "token_hmac, nonce, expires_at, consumed_at, created_at, resolved_at, "
    "conversation_id, attempt_count, route, sensitivity, agent_id"
)


def _add_pending_approvals_columns(conn: sqlite3.Connection) -> None:
    """EXPAND aditivo idempotente. Cada ALTER en try/except OperationalError
    ("duplicate column name") — sqlite no tiene IF NOT EXISTS para columnas.

    Debe correr ANTES de `_recreate_pending_approvals_status_check_if_needed`:
    garantiza que la tabla vieja expone TODAS las columnas que el INSERT SELECT
    de la recreación va a copiar.
    """
    for stmt in _PENDING_APPROVALS_ADD_COLUMNS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # columna ya existe — idempotente


def _recreate_pending_approvals_status_check_if_needed(
    conn: sqlite3.Connection,
) -> None:
    """🔒 Recreación controlada del CHECK de `status` → admite 'expired'.

    SQLite no permite ALTER ... DROP/MODIFY CONSTRAINT: se recrea la tabla con el
    patrón oficial (CREATE new → INSERT SELECT → DROP old → RENAME) dentro de UNA
    transacción. Preserva TODAS las filas (INSERT SELECT explícito por nombre) y
    recrea los índices (el DROP TABLE los borra) en el bloque de índices de
    ensure_*.

    Idempotencia por INSPECCIÓN DEL PROPIO CHECK: si el DDL de la tabla en
    sqlite_master ya contiene 'expired', no-op. NO usa `PRAGMA user_version`
    (shell-state.db lo comparte con el esquema `tasks`, que ya lo posee 1→4).

    `PRAGMA foreign_keys` se pone OFF durante el RENAME (defensivo, mismo patrón
    que la recreación de `agent_tasks`) y se restaura al valor previo al terminar.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='pending_approvals'"
    ).fetchone()
    if row is None or row[0] is None:
        return  # la tabla aún no existe (el CREATE corre antes → no debería pasar)
    if "'expired'" in row[0]:
        return  # ya migrada: el CHECK ya admite 'expired' — no-op idempotente

    fk_prev = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TABLE IF EXISTS pending_approvals_new")
            # `execute` (no `executescript`): executescript haría COMMIT implícito
            # y cerraría la transacción. Es UN solo statement CREATE TABLE.
            conn.execute(_DDL_PENDING_APPROVALS_NEW)
            conn.execute(
                f"INSERT INTO pending_approvals_new ({_PENDING_APPROVALS_COLUMNS}) "
                f"SELECT {_PENDING_APPROVALS_COLUMNS} FROM pending_approvals"
            )
            conn.execute("DROP TABLE pending_approvals")
            conn.execute(
                "ALTER TABLE pending_approvals_new RENAME TO pending_approvals"
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if fk_prev else 'OFF'}")


# ── DDL: agent_access_scopes (Enterprise Fase 2 Phase 1 — runtime-only
# per-agent native-tool access scope; NO cloud/config-sync in this phase) ──
# Una fila por (tenant_id, agent_id): el PK compuesto hace que upsert() (T042,
# SqliteAgentAccessScopeRepo) reemplace SIEMPRE la única fila del agente en vez
# de acumular historial. `enforced=0` (default) es el estado de una instancia
# local/sin política cloud: no gobierna nada — el runtime se comporta EXACTAMENTE
# como antes de que esta tabla existiera (fail-open leído por
# security_hook._check_agent_access_scope y nous_engine._apply_agent_filter).
_DDL_AGENT_ACCESS_SCOPES = """
CREATE TABLE IF NOT EXISTS agent_access_scopes (
    tenant_id            TEXT NOT NULL,
    agent_id             TEXT NOT NULL,
    scope_id             TEXT NOT NULL,
    native_tools         TEXT NOT NULL DEFAULT '[]',
    policy_overlay       TEXT NOT NULL DEFAULT '{}',
    views                TEXT NOT NULL DEFAULT '[]',
    cerebro_unrestricted INTEGER NOT NULL DEFAULT 1,
    enforced             INTEGER NOT NULL DEFAULT 0,
    updated_by           INTEGER NOT NULL,
    managed_by           TEXT,
    approval_tier        TEXT NOT NULL DEFAULT 'standard',
    authorized_mcp_servers TEXT NOT NULL DEFAULT '[]',
    updated_at           TEXT NOT NULL,
    PRIMARY KEY (tenant_id, agent_id)
);
CREATE INDEX IF NOT EXISTS agent_access_scopes_agent_idx
    ON agent_access_scopes (agent_id);
"""


def ensure_capabilities_schema(conn: sqlite3.Connection) -> None:
    """Aplica el DDL de `pending_approvals` + `agent_access_scopes` sobre una
    conexión abierta.

    Idempotente: CREATE TABLE/INDEX IF NOT EXISTS + ALTER en try/except + una
    recreación controlada del CHECK de `status` bajo guard por inspección del
    propio CHECK. Forward-only; re-ejecutar no destruye datos ni lanza.
    """
    conn.executescript(_DDL_PENDING_APPROVALS)
    # EXPAND aditivo (columnas ALTER) — ANTES de la recreación de status.
    _add_pending_approvals_columns(conn)
    # Recreación del CHECK de status para admitir 'expired' (DBs con CHECK viejo).
    # El DROP TABLE interno borra los índices → se recrean en el bloque de abajo.
    _recreate_pending_approvals_status_check_if_needed(conn)
    # (Re)crea todos los índices: idempotente + repuebla tras la recreación.
    conn.executescript(_DDL_PENDING_APPROVALS_INDEXES)
    # Enterprise Fase 2 Phase 1: runtime-only per-agent access scope table.
    conn.executescript(_DDL_AGENT_ACCESS_SCOPES)
    # EXPAND (per-role governance, 2026-07-05): approval_tier column for DBs
    # created before it existed. Idempotent — no-op if the column is present.
    try:
        conn.execute(
            "ALTER TABLE agent_access_scopes "
            "ADD COLUMN approval_tier TEXT NOT NULL DEFAULT 'standard'"
        )
    except sqlite3.OperationalError:
        pass
    # EXPAND (bundle-authorized MCP admission, 2026-07-07): authorized_mcp_servers
    # column for DBs created before it existed. Idempotent — no-op if present.
    try:
        conn.execute(
            "ALTER TABLE agent_access_scopes "
            "ADD COLUMN authorized_mcp_servers TEXT NOT NULL DEFAULT '[]'"
        )
    except sqlite3.OperationalError:
        pass
