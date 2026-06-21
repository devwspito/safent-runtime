"""Esquema SQLite FIRMADO del BC `tasks` (data-model.md, features 005 + 006).

`ensure_tasks_schema(conn)` aplica, de forma idempotente sobre una conexión
abierta:

  - PRAGMAs de conexión (WAL / synchronous=NORMAL / busy_timeout / foreign_keys),
  - DDL FIRMADO P0 de `agent_tasks` (cola durable: estado del motor stateless,
    NFR-002) con sus 4 CHECK de invariantes I1-I4 y sus 4 índices parciales
    (dedup / dequeue / lease / retry),
  - DDL FIRMADO de `agent_runtime_state` (singleton kill-switch) + el
    `INSERT OR IGNORE` del estado inicial `running`,
  - **Migración 006 (P1)** — EXPAND aditivo + recreación controlada del CHECK,
    bajo guard `PRAGMA user_version`. Ver §Migración 006 más abajo.

Patrón replicado de `SQLiteConsentRepository`: conexión por llamada en
autocommit (`isolation_level=None`), DDL on-connect con `CREATE ... IF NOT
EXISTS`. Re-ejecutar no destruye ni falla.

NOTA DE SEGURIDAD (threat-model TOP-2/CTRL-10): `agent_tasks` incluye
`enqueued_by` (authZ de quién da órdenes al agente), `payload_signature`
(anti-TOCTOU) e `idempotency_key` (anti doble-efecto en reintento). El DDL
está firmado por el usuario; no alterar columnas ni CHECK sin re-firma.

──────────────────────────────────────────────────────────────────────────────
Migración 006 (P1) — chat como WorkItem en `agent_tasks` (data-model 006 §3-§4):

  EXPAND (aditivo, backwards-compatible — el código P0 sigue funcionando):
    - ADD COLUMN kind TEXT NOT NULL DEFAULT 'autonomous'  (metadata-only)
    - ADD COLUMN worker_id TEXT
    - ADD COLUMN conversation_id TEXT
    - índices idx_agent_tasks_conversation / idx_agent_tasks_worker_active

  RECREACIÓN CONTROLADA (🔒 FIRMADA — data-model 006 §8 puntos 3-4):
    SQLite no permite ALTER ... DROP/MODIFY CONSTRAINT. Para admitir
    'chat_message' en el CHECK de `trigger_kind` (P0 sólo permitía
    'manual_enqueue') y añadir las invariantes I5/I6, se recrea la tabla con el
    patrón oficial (CREATE new → INSERT SELECT → DROP old → RENAME) dentro de
    UNA transacción. Preserva TODOS los CHECK I1-I4 firmados de P0 (anti-éxito-
    alucinado incluido). Guard `PRAGMA user_version`: sólo corre si la DB está
    por debajo de P1; re-ejecutar = no-op.

  Versionado (data-model 006 §A7 / OQ-1): P0 NO setea `user_version` (queda 0).
  P1 lo adopta: tras aplicar el esquema, `user_version = _SCHEMA_VERSION_P1`.
"""

from __future__ import annotations

import sqlite3

# ── Versionado de esquema (guard idempotente de migración) ──────────────────
# P0 dejaba `user_version` en 0 (no lo seteaba). P1 lo adopta: la baseline P0
# se documenta como versión 1; P1 (chat-en-agent_tasks) es la versión 2.
# P2 (triggers default-deny, data-model 007) es la versión 3.
_SCHEMA_VERSION_P0: int = 1
_SCHEMA_VERSION_P1: int = 2
_SCHEMA_VERSION_P2: int = 3
_SCHEMA_VERSION_P3: int = 4

# Pragmas de conexión — afectan a TODOS los procesos que abren el fichero
# (firma del usuario, data-model §"Decisiones irreversibles" punto 8).
_PRAGMAS = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
"""

# DDL FIRMADO P0 — agent_tasks (data-model 005 §"DDL — agent_tasks").
# Estado de CREACIÓN sobre una DB virgen: CHECK trigger_kind sólo
# 'manual_enqueue'. La migración 006 lo recrea para admitir 'chat_message'.
_DDL_AGENT_TASKS_P0 = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    -- Identidad
    task_id                  TEXT PRIMARY KEY,

    -- Origen del disparo (FR-002) — en P0 SOLO 'manual_enqueue'
    trigger_kind             TEXT NOT NULL
        CHECK (trigger_kind IN ('manual_enqueue')),

    -- AuthZ: quién encoló (threat-model TOP-2/CTRL-10) — sin esto, rechazo fail-closed
    enqueued_by              TEXT NOT NULL,
    payload_signature        TEXT,

    -- Tenant / operador bajo cuyo consent opera la unidad
    tenant_id                TEXT,
    operator_id              TEXT NOT NULL,

    -- Payload de la WorkItem (qué tiene que hacer el agente)
    instruction              TEXT NOT NULL,
    payload_json             TEXT NOT NULL DEFAULT '{}',

    -- Ciclo de vida explícito (FR-004) — un solo estado a la vez
    status                   TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'in_progress', 'completed',
            'failed', 'pending_approval', 'rejected'
        )),

    -- Dedup (FR-005 / SC-007)
    dedup_key                TEXT,

    -- Prioridad / orden de dequeue (FR-003)
    priority                 INTEGER NOT NULL DEFAULT 0,

    -- Dequeue atómico + reconciliación (FR-003 / FR-007)
    claim_token              TEXT,
    claimed_at               TEXT,
    lease_expires_at         TEXT,
    heartbeat_at             TEXT,

    -- Idempotencia de efecto en reintento/reconciliación (RECON-1/CTRL-11)
    idempotency_key          TEXT,

    -- Reintento idempotente (FR-006)
    retry_count              INTEGER NOT NULL DEFAULT 0,
    max_retries              INTEGER NOT NULL DEFAULT 5,
    next_attempt_at          TEXT,
    last_error               TEXT,

    -- Trazabilidad anti-éxito-alucinado (SC-001 / FR-020)
    execution_audit_entry_id TEXT,
    execution_head_hash      TEXT,

    -- Marcas temporales
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,

    -- ── INVARIANTES A NIVEL DE ESQUEMA ──────────────────────────────────

    -- I1: 'completed' es IMPOSIBLE sin evidencia de ejecución real (SC-001).
    CHECK (
        status <> 'completed'
        OR (execution_audit_entry_id IS NOT NULL
            AND execution_head_hash   IS NOT NULL)
    ),
    -- I2: estado terminal => sin lease/claim vivos.
    CHECK (
        status NOT IN ('completed','failed','rejected')
        OR (claim_token IS NULL AND lease_expires_at IS NULL)
    ),
    -- I3: 'in_progress' => tiene claim y lease (para reconciliación).
    CHECK (
        status <> 'in_progress'
        OR (claim_token IS NOT NULL AND claimed_at IS NOT NULL
            AND lease_expires_at IS NOT NULL)
    ),
    -- I4: contadores de reintento coherentes.
    CHECK (retry_count >= 0 AND max_retries >= 0 AND retry_count <= max_retries)
);
"""

# Índices FIRMADOS de P0 + nuevos de P1. Idempotentes (IF NOT EXISTS). Se
# (re)crean tras la recreación de la tabla — la recreación los borra al hacer
# DROP TABLE, así que recrearlos aquí los repuebla siempre.
_DDL_AGENT_TASKS_INDEXES = """
-- ── ÍNDICES P0 (FIRMADOS) — cada uno justificado por una query ───────────

-- Dedup (FR-005 / SC-007): N encolados con misma dedup_key entre vivas => 1 fila.
CREATE UNIQUE INDEX IF NOT EXISTS agent_tasks_dedup_key_active_unique
    ON agent_tasks (dedup_key)
    WHERE dedup_key IS NOT NULL
      AND status NOT IN ('completed','failed','rejected');

-- Dequeue (FR-003): siguiente 'pending' lista, por prioridad y orden de llegada.
CREATE INDEX IF NOT EXISTS agent_tasks_dequeue_idx
    ON agent_tasks (status, priority DESC, created_at ASC)
    WHERE status = 'pending';

-- Reconciliación (FR-007 / SC-003): 'in_progress' huérfanas por lease vencido.
CREATE INDEX IF NOT EXISTS agent_tasks_lease_idx
    ON agent_tasks (lease_expires_at)
    WHERE status = 'in_progress';

-- Reintento (FR-006): 'failed' reintentables cuyo backoff venció.
CREATE INDEX IF NOT EXISTS agent_tasks_retry_idx
    ON agent_tasks (next_attempt_at)
    WHERE status = 'failed';

-- ── ÍNDICES P1 (data-model 006 §3.1 / §6) ───────────────────────────────

-- Q: "tareas de esta conversación, en orden" (mirror + reconexión de stream).
--    SELECT ... WHERE conversation_id = ? ORDER BY created_at
CREATE INDEX IF NOT EXISTS idx_agent_tasks_conversation
    ON agent_tasks (conversation_id, created_at)
    WHERE conversation_id IS NOT NULL;

-- Q: "tareas in_progress de un worker dado" (reconciliación tras reinicio,
--    SC-010): re-encolar lo que tenía el worker caído.
--    SELECT task_id WHERE status='in_progress' AND worker_id = ?
CREATE INDEX IF NOT EXISTS idx_agent_tasks_worker_active
    ON agent_tasks (worker_id)
    WHERE status = 'in_progress';
"""

# Columnas nuevas de P1 (ALTER ADD COLUMN). Cada una en su propio statement
# para tratar OperationalError ("duplicate column name") de forma idempotente.
_P1_ADD_COLUMNS: tuple[str, ...] = (
    # kind: clase de la unidad de trabajo. DEFAULT constante => seguro en
    # caliente, metadata-only, no reescribe (filas P0 quedan 'autonomous').
    "ALTER TABLE agent_tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'autonomous'",
    # worker_id: qué worker del pool tiene la tarea in_progress (operacional).
    "ALTER TABLE agent_tasks ADD COLUMN worker_id TEXT",
    # conversation_id: primera clase para chat (join al mirror + índice).
    "ALTER TABLE agent_tasks ADD COLUMN conversation_id TEXT",
)

# 🔒 RECREACIÓN FIRMADA — tabla `agent_tasks` con el CHECK relajado + I5/I6.
# Mismas columnas P0 (idénticas) + kind/worker_id/conversation_id. TODOS los
# CHECK I1-I4 de P0 se replican TEXTUALMENTE; I5/I6 son aditivos. Se ejecuta
# dentro de una transacción (CREATE new → INSERT SELECT → DROP old → RENAME)
# bajo guard de versión. NO se pierde ninguna fila: el DROP opera sobre datos
# ya copiados en agent_tasks_new dentro de la MISMA transacción.
_DDL_AGENT_TASKS_NEW = """
CREATE TABLE agent_tasks_new (
    -- Identidad
    task_id                  TEXT PRIMARY KEY,

    -- Origen del disparo (FR-002) — P1 admite 'chat_message' (data-model 006 §8.3)
    trigger_kind             TEXT NOT NULL
        CHECK (trigger_kind IN ('manual_enqueue', 'chat_message')),

    -- AuthZ: quién encoló (threat-model TOP-2/CTRL-10) — sin esto, rechazo fail-closed
    enqueued_by              TEXT NOT NULL,
    payload_signature        TEXT,

    -- Tenant / operador bajo cuyo consent opera la unidad
    tenant_id                TEXT,
    operator_id              TEXT NOT NULL,

    -- Payload de la WorkItem (qué tiene que hacer el agente)
    instruction              TEXT NOT NULL,
    payload_json             TEXT NOT NULL DEFAULT '{}',

    -- Ciclo de vida explícito (FR-004) — un solo estado a la vez
    status                   TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'in_progress', 'completed',
            'failed', 'pending_approval', 'rejected'
        )),

    -- Dedup (FR-005 / SC-007)
    dedup_key                TEXT,

    -- Prioridad / orden de dequeue (FR-003)
    priority                 INTEGER NOT NULL DEFAULT 0,

    -- Dequeue atómico + reconciliación (FR-003 / FR-007)
    claim_token              TEXT,
    claimed_at               TEXT,
    lease_expires_at         TEXT,
    heartbeat_at             TEXT,

    -- Idempotencia de efecto en reintento/reconciliación (RECON-1/CTRL-11)
    idempotency_key          TEXT,

    -- Reintento idempotente (FR-006)
    retry_count              INTEGER NOT NULL DEFAULT 0,
    max_retries              INTEGER NOT NULL DEFAULT 5,
    next_attempt_at          TEXT,
    last_error               TEXT,

    -- Trazabilidad anti-éxito-alucinado (SC-001 / FR-020)
    execution_audit_entry_id TEXT,
    execution_head_hash      TEXT,

    -- ── P1: clase de WorkItem + observabilidad de pool + chat ────────────
    kind                     TEXT NOT NULL DEFAULT 'autonomous'
        CHECK (kind IN ('autonomous', 'chat_message')),
    worker_id                TEXT,
    conversation_id          TEXT,

    -- Marcas temporales
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,

    -- ── INVARIANTES A NIVEL DE ESQUEMA ──────────────────────────────────

    -- I1: 'completed' es IMPOSIBLE sin evidencia de ejecución real (SC-001).
    CHECK (
        status <> 'completed'
        OR (execution_audit_entry_id IS NOT NULL
            AND execution_head_hash   IS NOT NULL)
    ),
    -- I2: estado terminal => sin lease/claim vivos.
    CHECK (
        status NOT IN ('completed','failed','rejected')
        OR (claim_token IS NULL AND lease_expires_at IS NULL)
    ),
    -- I3: 'in_progress' => tiene claim y lease (para reconciliación).
    CHECK (
        status <> 'in_progress'
        OR (claim_token IS NOT NULL AND claimed_at IS NOT NULL
            AND lease_expires_at IS NOT NULL)
    ),
    -- I4: contadores de reintento coherentes.
    CHECK (retry_count >= 0 AND max_retries >= 0 AND retry_count <= max_retries),

    -- I5 (P1): un chat_message SIEMPRE lleva conversation_id; lo autónomo no exige.
    CHECK (
        kind <> 'chat_message'
        OR conversation_id IS NOT NULL
    ),
    -- I6 (P1): 'in_progress' => tiene worker_id asignado (reconciliación dirigida).
    CHECK (
        status <> 'in_progress'
        OR worker_id IS NOT NULL
    )
);
"""

# Copia explícita de columnas P0 (las nuevas ya están pobladas por el EXPAND
# previo; columnas en orden estable para que el SELECT case con el INSERT).
_P0_COLUMNS = (
    "task_id, trigger_kind, enqueued_by, payload_signature, tenant_id, "
    "operator_id, instruction, payload_json, status, dedup_key, priority, "
    "claim_token, claimed_at, lease_expires_at, heartbeat_at, idempotency_key, "
    "retry_count, max_retries, next_attempt_at, last_error, "
    "execution_audit_entry_id, execution_head_hash, "
    "kind, worker_id, conversation_id, created_at, updated_at"
)

# DDL FIRMADO — agent_runtime_state (data-model 005 §"DDL — agent_runtime_state").
_DDL_AGENT_RUNTIME_STATE = """
CREATE TABLE IF NOT EXISTS agent_runtime_state (
    id           TEXT PRIMARY KEY DEFAULT 'singleton'
        CHECK (id = 'singleton'),
    loop_state   TEXT NOT NULL DEFAULT 'running'
        CHECK (loop_state IN ('running','paused')),
    reason       TEXT,
    changed_by   TEXT,
    updated_at   TEXT NOT NULL
);

INSERT OR IGNORE INTO agent_runtime_state (id, loop_state, updated_at)
VALUES ('singleton', 'running', strftime('%Y-%m-%dT%H:%M:%fZ','now'));
"""

# ════════════════════════════════════════════════════════════════════════════
# Migración 007 (P2) — triggers default-deny (data-model 007 §3-§4).
#
# El corazón es DEFAULT-DENY: la allow-list de orígenes auto-disparadores
# (`authorized_trigger_instances`) NACE VACÍA. Sin una fila firmada por un
# admin, NINGUNA fuente automática (timer / system_event / self_enqueue) puede
# encolar. Este DDL SOLO AÑADE: dos tablas nuevas, una columna FK en
# `agent_tasks`, y una recreación controlada del CHECK de `trigger_kind`.
# Preserva TEXTUALMENTE I1-I6 firmados en P0/P1 (anti-éxito-alucinado incluido).
# ════════════════════════════════════════════════════════════════════════════

# DDL FIRMADO P2 — authorized_trigger_types (catálogo enum de TIPOS de origen).
# Tabla casi-estática (≤ 3 filas). Define la POLÍTICA BASE de cada tipo.
# enabled_by_default SIEMPRE 0 con CHECK que lo fija → sembrar el tipo NO
# habilita nada (DEFAULT-DENY a nivel de fila). La siembra es INSERT OR IGNORE:
# re-ejecutar = no-op; sembrar TIPOS NO crea NINGUNA instancia.
_DDL_AUTHORIZED_TRIGGER_TYPES = """
CREATE TABLE IF NOT EXISTS authorized_trigger_types (
    -- PK natural: el nombre del tipo es estable y único (whitelist positiva).
    trigger_type        TEXT PRIMARY KEY
        CHECK (trigger_type IN ('timer', 'system_event', 'self_enqueue')),

    -- Validación de scope que exige este tipo (cómo se interpreta scope_value
    -- de la instancia). Enum cerrado; la app resuelve el validador por su valor.
    scope_validation    TEXT NOT NULL
        CHECK (scope_validation IN (
            'cron_expression', 'event_class', 'parent_task_kind'
        )),

    -- Techo de riesgo máximo que una INSTANCIA de este tipo puede declarar.
    max_risk_level      TEXT NOT NULL
        CHECK (max_risk_level IN ('low', 'high')),

    -- DEFAULT-DENY DURO: SIEMPRE 0. El CHECK lo hace imposible de poner a 1.
    enabled_by_default  INTEGER NOT NULL DEFAULT 0
        CHECK (enabled_by_default = 0),

    -- Descripción humana del tipo (para la UI de autorización).
    description         TEXT NOT NULL DEFAULT '',

    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- Siembra idempotente del catálogo. Sembrar TIPOS NO habilita nada → la
-- allow-list de instancias sigue VACÍA.
INSERT OR IGNORE INTO authorized_trigger_types
    (trigger_type, scope_validation, max_risk_level, description,
     created_at, updated_at)
VALUES
    ('timer',        'cron_expression',  'high', 'Disparo por calendario/timer',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('system_event', 'event_class',      'high', 'Disparo por evento del SO (lista cerrada)',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('self_enqueue', 'parent_task_kind', 'low',  'Auto-encolado de seguimiento (cap cascada 1)',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'));
"""

# DDL FIRMADO P2 — authorized_trigger_instances (allow-list firmada; VACÍA por
# defecto). Una fila = un origen concreto autorizado a encolar. SIN filas = 0
# auto-disparos (SC-013 / I14). NO se siembra ninguna. Incluye I11 como CHECK.
_DDL_AUTHORIZED_TRIGGER_INSTANCES = """
CREATE TABLE IF NOT EXISTS authorized_trigger_instances (
    instance_id             TEXT PRIMARY KEY,          -- UUID (TEXT)

    -- Tipo de origen (FK a la whitelist de tipos). RESTRICT: no borrar un tipo
    -- con instancias; no instanciar tipos no catalogados.
    trigger_type            TEXT NOT NULL
        REFERENCES authorized_trigger_types(trigger_type)
        ON UPDATE RESTRICT ON DELETE RESTRICT,

    -- Ámbito: qué activa concretamente este origen (interpretado según
    -- scope_validation del tipo). NOT NULL: un origen sin ámbito → fail-closed.
    scope_value             TEXT NOT NULL,

    -- Capacidades que este origen puede solicitar (JSON array de Capability).
    allowed_capabilities_json TEXT NOT NULL DEFAULT '[]',

    -- Techo de riesgo de este origen (<= max_risk_level del tipo; I13 en app).
    risk_ceiling            TEXT NOT NULL DEFAULT 'low'
        CHECK (risk_ceiling IN ('low', 'high')),

    -- Presupuesto por hora de este origen (OQ-1: columna por-origen, auditable
    -- junto a la firma; default 10 disparos/hora — el gate lo enforza).
    hourly_budget           INTEGER NOT NULL DEFAULT 10
        CHECK (hourly_budget >= 0),

    -- ── AuthZ / firma (CWE-862, no-repudio) ──────────────────────────────
    -- Identidad del admin que autorizó, del canal autenticado (D-Bus UID),
    -- NUNCA del contenido. Es el enqueued_by efectivo de las tareas disparadas.
    created_by_admin_uuid   TEXT NOT NULL,
    authorized_at           TEXT NOT NULL,
    approval_signature      TEXT NOT NULL,             -- no-repudio del admin

    -- ── Estado / revocación (kill por-origen, FR-018) ────────────────────
    enabled                 INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1)),
    revoked_at              TEXT,                       -- NULL = vigente
    revoked_by_admin_uuid   TEXT,

    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,

    -- I11: revocado <=> deshabilitado (coherencia del kill por-origen).
    CHECK (
        (enabled = 1 AND revoked_at IS NULL)
        OR (enabled = 0 AND revoked_at IS NOT NULL)
    )
);

-- HOT-PATH del gate fail-closed (is_authorized): "¿hay un origen VIGENTE de
-- este tipo+ámbito?". Índice parcial sobre vigentes.
CREATE INDEX IF NOT EXISTS idx_authorized_trigger_instances_lookup
    ON authorized_trigger_instances (trigger_type, scope_value)
    WHERE enabled = 1;

-- Q: "lista de orígenes de un tipo" (supervisión / UI de autorización).
CREATE INDEX IF NOT EXISTS idx_authorized_trigger_instances_type
    ON authorized_trigger_instances (trigger_type, authorized_at);
"""

# Columnas nuevas de P2 (ALTER ADD COLUMN). EXPAND backwards-compatible: la FK
# y el CHECK I10 se materializan en la recreación (igual que P1 con kind/etc).
_P2_ADD_COLUMNS: tuple[str, ...] = (
    # trigger_instance_id: FK al origen autorizado (NULL si manual/chat).
    # nullable, metadata-only, instantáneo. Filas P1 quedan NULL (correcto).
    "ALTER TABLE agent_tasks ADD COLUMN trigger_instance_id TEXT",
)

# 🔒 RECREACIÓN FIRMADA P2 — `agent_tasks` con el CHECK de `trigger_kind`
# relajado (+timer / system_event / self_enqueue), la FK trigger_instance_id, e
# I10. Mismas columnas P0/P1 (idénticas) + trigger_instance_id. TODOS los CHECK
# I1-I6 se replican TEXTUALMENTE; I10 es aditivo. Se ejecuta dentro de una
# transacción (CREATE new → INSERT SELECT → DROP old → RENAME) bajo guard de
# versión. `kind` NO se toca (OQ-2): auto-disparadas son kind='autonomous'.
_DDL_AGENT_TASKS_NEW_P2 = """
CREATE TABLE agent_tasks_new (
    -- Identidad
    task_id                  TEXT PRIMARY KEY,

    -- Origen del disparo (FR-002) — P2 admite los 3 tipos auto (whitelist+I10)
    trigger_kind             TEXT NOT NULL
        CHECK (trigger_kind IN (
            'manual_enqueue', 'chat_message',
            'timer', 'system_event', 'self_enqueue'
        )),

    -- AuthZ: quién encoló (threat-model TOP-2/CTRL-10) — sin esto, rechazo fail-closed
    enqueued_by              TEXT NOT NULL,
    payload_signature        TEXT,

    -- Tenant / operador bajo cuyo consent opera la unidad
    tenant_id                TEXT,
    operator_id              TEXT NOT NULL,

    -- Payload de la WorkItem (qué tiene que hacer el agente)
    instruction              TEXT NOT NULL,
    payload_json             TEXT NOT NULL DEFAULT '{}',

    -- Ciclo de vida explícito (FR-004) — un solo estado a la vez
    status                   TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'in_progress', 'completed',
            'failed', 'pending_approval', 'rejected'
        )),

    -- Dedup (FR-005 / SC-007)
    dedup_key                TEXT,

    -- Prioridad / orden de dequeue (FR-003)
    priority                 INTEGER NOT NULL DEFAULT 0,

    -- Dequeue atómico + reconciliación (FR-003 / FR-007)
    claim_token              TEXT,
    claimed_at               TEXT,
    lease_expires_at         TEXT,
    heartbeat_at             TEXT,

    -- Idempotencia de efecto en reintento/reconciliación (RECON-1/CTRL-11)
    idempotency_key          TEXT,

    -- Reintento idempotente (FR-006)
    retry_count              INTEGER NOT NULL DEFAULT 0,
    max_retries              INTEGER NOT NULL DEFAULT 5,
    next_attempt_at          TEXT,
    last_error               TEXT,

    -- Trazabilidad anti-éxito-alucinado (SC-001 / FR-020)
    execution_audit_entry_id TEXT,
    execution_head_hash      TEXT,

    -- ── P1: clase de WorkItem + observabilidad de pool + chat ────────────
    kind                     TEXT NOT NULL DEFAULT 'autonomous'
        CHECK (kind IN ('autonomous', 'chat_message')),
    worker_id                TEXT,
    conversation_id          TEXT,

    -- ── P2: origen autorizado que disparó la tarea (NULL si manual/chat) ──
    trigger_instance_id      TEXT
        REFERENCES authorized_trigger_instances(instance_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,

    -- Marcas temporales
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,

    -- ── INVARIANTES A NIVEL DE ESQUEMA ──────────────────────────────────

    -- I1: 'completed' es IMPOSIBLE sin evidencia de ejecución real (SC-001).
    CHECK (
        status <> 'completed'
        OR (execution_audit_entry_id IS NOT NULL
            AND execution_head_hash   IS NOT NULL)
    ),
    -- I2: estado terminal => sin lease/claim vivos.
    CHECK (
        status NOT IN ('completed','failed','rejected')
        OR (claim_token IS NULL AND lease_expires_at IS NULL)
    ),
    -- I3: 'in_progress' => tiene claim y lease (para reconciliación).
    CHECK (
        status <> 'in_progress'
        OR (claim_token IS NOT NULL AND claimed_at IS NOT NULL
            AND lease_expires_at IS NOT NULL)
    ),
    -- I4: contadores de reintento coherentes.
    CHECK (retry_count >= 0 AND max_retries >= 0 AND retry_count <= max_retries),

    -- I5 (P1): un chat_message SIEMPRE lleva conversation_id; lo autónomo no exige.
    CHECK (
        kind <> 'chat_message'
        OR conversation_id IS NOT NULL
    ),
    -- I6 (P1): 'in_progress' => tiene worker_id asignado (reconciliación dirigida).
    CHECK (
        status <> 'in_progress'
        OR worker_id IS NOT NULL
    ),

    -- I10 (P2): un disparo automático SIEMPRE tiene un origen autorizado; uno
    -- manual/chat NUNCA tiene origen automático (atribución obligatoria).
    CHECK (
        (trigger_kind IN ('timer','system_event','self_enqueue')
            AND trigger_instance_id IS NOT NULL)
        OR
        (trigger_kind IN ('manual_enqueue','chat_message')
            AND trigger_instance_id IS NULL)
    )
);
"""

# Copia explícita de columnas al recrear en P2 (P0/P1 + trigger_instance_id,
# ya poblada/NULL por el EXPAND previo). Orden estable: SELECT case con INSERT.
_P2_COLUMNS = (
    "task_id, trigger_kind, enqueued_by, payload_signature, tenant_id, "
    "operator_id, instruction, payload_json, status, dedup_key, priority, "
    "claim_token, claimed_at, lease_expires_at, heartbeat_at, idempotency_key, "
    "retry_count, max_retries, next_attempt_at, last_error, "
    "execution_audit_entry_id, execution_head_hash, "
    "kind, worker_id, conversation_id, trigger_instance_id, created_at, updated_at"
)

# Índice FK de agent_tasks (P2). SQLite NO crea índice automático sobre FKs.
# Q: "tareas de un origen" (revocación que ve tareas en vuelo) + presupuesto/hora
#    WHERE trigger_instance_id=? AND created_at>? . Parcial: solo auto-disparadas.
_DDL_AGENT_TASKS_TRIGGER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_agent_tasks_trigger_instance
    ON agent_tasks (trigger_instance_id, created_at)
    WHERE trigger_instance_id IS NOT NULL;
"""


# ════════════════════════════════════════════════════════════════════════════
# Migración P3 — calendario de tareas per-agent (feat scheduled-tasks).
#
# EXPAND ADITIVO sobre authorized_trigger_instances (backwards-compatible):
#   - target_agent_id TEXT   — el agente destino (NULL = activo en el momento)
#   - task_instruction TEXT  — instrucción que se encola al disparar (default '')
#   - one_shot INTEGER       — 1 = auto-revoca tras la primera ejecución
#   - title TEXT             — etiqueta legible para el calendario
#
# DEFAULT-DENY preservado: NINGÚN valor nuevo invalida las invariantes I11 ni
# el CHECK de trigger_type / risk_ceiling / enabled / hourly_budget firmados en P2.
# Filas P2 existentes quedan con task_instruction='' / one_shot=0 / title='',
# que es comportamiento correcto (actúan igual que antes).
# ════════════════════════════════════════════════════════════════════════════

# ADD COLUMN idempotente — cada statement en try/except OperationalError.
# "duplicate column name" = ya aplicado (re-ejecución o bake con imagen actualizada).
_P3_ADD_COLUMNS: tuple[str, ...] = (
    # Agente destino. NULL = usar el agente activo en el momento del disparo.
    "ALTER TABLE authorized_trigger_instances ADD COLUMN target_agent_id TEXT",
    # Instrucción que el timer usa al encolar el work item. DEFAULT '' para que
    # las filas P2 sigan funcionando con el fallback "Timer scheduled task — scope=…".
    "ALTER TABLE authorized_trigger_instances ADD COLUMN task_instruction TEXT NOT NULL DEFAULT ''",
    # Bandera one-shot: 1 = el trigger se auto-revoca (enabled=0 / revoked_at)
    # después de la primera ejecución exitosa.
    "ALTER TABLE authorized_trigger_instances ADD COLUMN one_shot INTEGER NOT NULL DEFAULT 0",
    # Título legible para el calendario (IU). DEFAULT '' para filas P2 existentes.
    "ALTER TABLE authorized_trigger_instances ADD COLUMN title TEXT NOT NULL DEFAULT ''",
)


def _expand_authorized_triggers_p3(conn: sqlite3.Connection) -> None:
    """EXPAND aditivo P3: añade target_agent_id/task_instruction/one_shot/title.

    Idempotente: cada ALTER en try/except OperationalError. Re-ejecutar = no-op.
    No toca agent_tasks — SOLO authorized_trigger_instances.
    """
    for stmt in _P3_ADD_COLUMNS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # columna ya presente — idempotente


def ensure_tasks_schema(conn: sqlite3.Connection) -> None:
    """Aplica PRAGMAs + DDL FIRMADO de `tasks` + migración 006 (P1).

    Idempotente: `CREATE TABLE/INDEX IF NOT EXISTS`, `INSERT OR IGNORE`, ALTER
    en try/except y recreación bajo guard `PRAGMA user_version`. Forward-only;
    re-ejecutar no destruye datos ni lanza.
    """
    conn.executescript(_PRAGMAS)
    conn.executescript(_DDL_AGENT_TASKS_P0)
    conn.executescript(_DDL_AGENT_RUNTIME_STATE)

    # P2 tablas nuevas ANTES de la recreación: `agent_tasks_new` declara una FK
    # a `authorized_trigger_instances`, que debe existir al crear la tabla.
    # Tablas nuevas (catálogo + allow-list vacía) → 0 impacto, idempotentes.
    conn.executescript(_DDL_AUTHORIZED_TRIGGER_TYPES)
    conn.executescript(_DDL_AUTHORIZED_TRIGGER_INSTANCES)

    _expand_agent_tasks_p1(conn)
    _recreate_agent_tasks_check_if_needed(conn)

    # P2 EXPAND + recreación bajo guard de versión (2→3). EXPAND añade
    # trigger_instance_id; la recreación relaja el CHECK de trigger_kind + I10.
    _expand_agent_tasks_p2(conn)
    _recreate_agent_tasks_check_p2_if_needed(conn)

    # Los índices (P0 + P1 + P2) se (re)crean al final: la recreación de la tabla
    # borra los índices al hacer DROP; este executescript los repuebla siempre.
    conn.executescript(_DDL_AGENT_TASKS_INDEXES)
    conn.executescript(_DDL_AGENT_TASKS_TRIGGER_INDEX)

    # P3 EXPAND sobre authorized_trigger_instances: target_agent_id/task_instruction/
    # one_shot/title. ADITIVO, no necesita guard de versión (no hay recreación de
    # tabla; ALTER ADD COLUMN es idempotente por try/except).
    _expand_authorized_triggers_p3(conn)


def _expand_agent_tasks_p1(conn: sqlite3.Connection) -> None:
    """EXPAND aditivo (data-model 006 §3.1): añade kind/worker_id/conversation_id.

    Cada ALTER en try/except OperationalError ("duplicate column name") para
    idempotencia: si la columna ya existe (re-ejecución o tabla ya recreada por
    P1), el ALTER falla y se ignora — el resto del esquema sigue su curso.
    """
    for stmt in _P1_ADD_COLUMNS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            # Columna ya presente => no-op idempotente.
            pass


def _recreate_agent_tasks_check_if_needed(conn: sqlite3.Connection) -> None:
    """🔒 Recreación controlada del CHECK de `trigger_kind` + I5/I6 (guard versión).

    SQLite no permite ALTER ... DROP/MODIFY CONSTRAINT. Para admitir
    'chat_message' y añadir I5/I6 se recrea la tabla con el patrón oficial
    (CREATE new → INSERT SELECT → DROP old → RENAME) dentro de UNA transacción.

    Guard: sólo corre si `user_version < _SCHEMA_VERSION_P1`. Tras completar,
    sube `user_version` a P1. Re-ejecutar = no-op (versión ya >= P1).
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= _SCHEMA_VERSION_P1:
        return

    # foreign_keys debe estar OFF durante el RENAME para no disparar acciones
    # referenciales de tablas que apunten a agent_tasks (p.ej. execution_contexts).
    # Se restaura al valor previo al terminar.
    fk_prev = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TABLE IF EXISTS agent_tasks_new")
            # `execute` (no `executescript`): executescript haría COMMIT
            # implícito y cerraría la transacción. _DDL_AGENT_TASKS_NEW es UN
            # solo statement CREATE TABLE.
            conn.execute(_DDL_AGENT_TASKS_NEW)
            conn.execute(
                f"INSERT INTO agent_tasks_new ({_P0_COLUMNS}) "
                f"SELECT {_P0_COLUMNS} FROM agent_tasks"
            )
            conn.execute("DROP TABLE agent_tasks")
            conn.execute("ALTER TABLE agent_tasks_new RENAME TO agent_tasks")
            # Sube la versión DENTRO de la transacción: o todo o nada.
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION_P1}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if fk_prev else 'OFF'}")


def _expand_agent_tasks_p2(conn: sqlite3.Connection) -> None:
    """EXPAND aditivo P2 (data-model 007 §4-E3): añade trigger_instance_id.

    nullable, metadata-only, backwards-compatible (filas P1 quedan NULL → eran
    manual/chat). Cada ALTER en try/except OperationalError ("duplicate column
    name") para idempotencia (re-ejecución o tabla ya recreada por P2).
    """
    for stmt in _P2_ADD_COLUMNS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            # Columna ya presente => no-op idempotente.
            pass


def _recreate_agent_tasks_check_p2_if_needed(conn: sqlite3.Connection) -> None:
    """🔒 Recreación controlada del CHECK de `trigger_kind` + FK + I10 (P2).

    Relaja el CHECK de `trigger_kind` para admitir timer/system_event/
    self_enqueue, materializa la FK `trigger_instance_id` y añade I10, con el
    patrón oficial (CREATE new → INSERT SELECT → DROP old → RENAME) dentro de
    UNA transacción. Preserva TEXTUALMENTE I1-I6 firmados en P0/P1. `kind` NO se
    toca (OQ-2): auto-disparadas son kind='autonomous'.

    Guard: sólo corre si `user_version < _SCHEMA_VERSION_P2` (encadena tras P1,
    que dejó la DB en 2). Tras completar, sube `user_version` a P2.
    Re-ejecutar = no-op (versión ya >= P2).
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= _SCHEMA_VERSION_P2:
        return

    # foreign_keys OFF durante el RENAME: igual que P1, evita disparar acciones
    # referenciales (p.ej. execution_contexts → agent_tasks). Se restaura luego.
    fk_prev = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TABLE IF EXISTS agent_tasks_new")
            conn.execute(_DDL_AGENT_TASKS_NEW_P2)
            conn.execute(
                f"INSERT INTO agent_tasks_new ({_P2_COLUMNS}) "
                f"SELECT {_P2_COLUMNS} FROM agent_tasks"
            )
            conn.execute("DROP TABLE agent_tasks")
            conn.execute("ALTER TABLE agent_tasks_new RENAME TO agent_tasks")
            # Sube la versión DENTRO de la transacción: o todo o nada.
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION_P2}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if fk_prev else 'OFF'}")
