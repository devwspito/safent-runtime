"""Esquema SQLite de la cadena de audit firmada (feature 005, T010).

`ensure_audit_chain_schema(conn)` aplica, de forma idempotente, el DDL de
`audit_chain_entries`: la tabla append-only que persiste las `AuditEntry`
firmadas por `AuditHashChainSigner` (CTRL-7/9). Hoy el firmer firma en memoria
sin persistir; esta tabla es el almacén durable que consumirá
`SqliteAuditRepository` (T017), implementación de `SignedAuditRepositoryPort`:

  - `append(entry)`     -> INSERT append-only (una fila por AuditEntry),
  - `head_hash_hex()`   -> `signed_payload_hash_hex` de la fila con mayor `seq`
                           (siembra `_last_hash` del firmer tras reinicio),
  - `load_chain(...)`   -> SELECT ordenado por `seq` (opcional filtro tenant_id)
                           para `verify_chain` (observabilidad, SC-006).

Las columnas reflejan 1:1 los campos de
`hermes.agents_os.application.audit_hash_chain.AuditEntry`
(entry_id, node_installation_id, tenant_id, timestamp, actor, audit_kind,
category, description, payload_hash_hex, prev_entry_hash_hex,
signed_payload_hash_hex, signature_hex). Se añaden:

  - `seq INTEGER`  — orden total de la cadena (AUTOINCREMENT, monótono):
    `head_hash_hex` = el de mayor `seq`; `load_chain` ordena por `seq`. Es el
    orden de inserción canónico, robusto frente a timestamps iguales.
  - `payload_json TEXT` — payload canónico que se firmó (sin PII en claro,
    constitución III). Permite re-verificar `payload_hash_hex` y reconstruir la
    entrada en `load_chain` (tasks.md T010).
  - `created_at TEXT`  — instante de persistencia (≠ `timestamp` lógico del
    evento, que lo fija el clock del firmer).

IMPORTANTE — coexistencia con `shell_server/audit_api.py`:
`audit_entries_view` YA existe como tabla de PROYECCIÓN de lectura (shape
estrecho: entry_id, timestamp, actor, audit_kind, category, description,
signature_short) que alimenta la UI. NO se redefine ni se toca su shape: aquí
solo se garantiza con `CREATE TABLE IF NOT EXISTS` (no-op si audit_api ya la
creó; la crea si esta función corre primero). `audit_chain_entries` es la fuente
de verdad firmada; `audit_entries_view` es la proyección. El repo (T017)
proyecta de una a otra.

Tipos coherentes con el store: timestamps TEXT ISO-8601 UTC, UUIDs TEXT,
JSON/hashes TEXT hex.
"""

from __future__ import annotations

import sqlite3

# Fuente de verdad firmada — append-only. Alineada con AuditEntry.
_DDL_AUDIT_CHAIN_ENTRIES = """
CREATE TABLE IF NOT EXISTS audit_chain_entries (
    -- Orden total de cadena: monótono, robusto ante timestamps iguales.
    seq                     INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identidad lógica estable de la entrada (AuditEntry.entry_id).
    entry_id                TEXT NOT NULL UNIQUE,

    -- Contexto (AuditEntry.node_installation_id / tenant_id — pueden ser NULL).
    node_installation_id    TEXT,
    tenant_id               TEXT,

    -- Instante lógico del evento (lo fija el clock del firmer).
    timestamp               TEXT NOT NULL,

    -- Quién y qué (AuditEntry.actor / audit_kind / category / description).
    actor                   TEXT NOT NULL,
    audit_kind              TEXT NOT NULL,
    category                TEXT,
    description             TEXT,

    -- Payload canónico firmado (sin PII en claro) — re-verificable.
    payload_json            TEXT NOT NULL DEFAULT '{}',

    -- Hashes y firma de la cadena (hex) — append-only, jamás se reescriben.
    payload_hash_hex        TEXT NOT NULL,
    prev_entry_hash_hex     TEXT NOT NULL,
    signed_payload_hash_hex TEXT NOT NULL,
    signature_hex           TEXT NOT NULL,

    -- Instante de persistencia (≠ timestamp lógico del evento).
    created_at              TEXT NOT NULL
);

-- Recorrido ordenado de la cadena (load_chain / verify_chain, SC-006) y
-- cálculo del head (mayor seq). Justifica el índice por seq.
CREATE INDEX IF NOT EXISTS audit_chain_entries_seq_idx
    ON audit_chain_entries (seq);

-- Carga de cadena filtrada por tenant (load_chain con tenant_id).
CREATE INDEX IF NOT EXISTS audit_chain_entries_tenant_seq_idx
    ON audit_chain_entries (tenant_id, seq);
"""

# Proyección de lectura para la UI. YA puede existir (creada por audit_api.py);
# `IF NOT EXISTS` la garantiza sin colisionar ni redefinir su shape estrecho.
_DDL_AUDIT_ENTRIES_VIEW = """
CREATE TABLE IF NOT EXISTS audit_entries_view (
    entry_id           TEXT PRIMARY KEY,
    timestamp          TEXT NOT NULL,
    actor              TEXT NOT NULL,
    audit_kind         TEXT NOT NULL,
    category           TEXT,
    description        TEXT NOT NULL,
    signature_short    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_ts_idx
    ON audit_entries_view (timestamp DESC);
"""


def ensure_audit_chain_schema(conn: sqlite3.Connection) -> None:
    """Aplica el DDL de `audit_chain_entries` (+ proyección) sobre una conexión.

    Idempotente: `CREATE TABLE/INDEX IF NOT EXISTS`. No redefine la proyección
    `audit_entries_view` de `audit_api.py` si ya existe (no-op). Re-ejecutar no
    destruye datos ni lanza.
    """
    conn.executescript(_DDL_AUDIT_CHAIN_ENTRIES)
    conn.executescript(_DDL_AUDIT_ENTRIES_VIEW)
