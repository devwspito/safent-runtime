-- migration: spec 003 T027 — SQLite variante personal-desktop.
-- research §13 — SQLite WAL synchronous=FULL en single-tenant.
-- TODAS las tablas en una sola migration (no hay versionado complejo).
--
-- Sintaxis adaptada de Postgres:
--   uuid       → TEXT (UUID v4 string)
--   jsonb      → TEXT (JSON-encoded)
--   text[]     → TEXT (JSON-encoded array)
--   enum       → TEXT con CHECK constraint
--   timestamptz → TEXT (ISO 8601)
--   gen_random_uuid() → generación lado-aplicación

PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;

-- =========================================================================
-- node_installations
-- =========================================================================
CREATE TABLE IF NOT EXISTS node_installations (
  node_installation_id   TEXT PRIMARY KEY,
  installed_at           TEXT NOT NULL,
  profile_kind           TEXT NOT NULL CHECK (profile_kind IN
                            ('workspace_only', 'personal_desktop', 'server')),
  operational_model      TEXT NOT NULL CHECK (operational_model IN
                            ('cloud_saas_managed', 'self_hosted')),
  current_image_version  TEXT NOT NULL,
  previous_image_version TEXT,
  active_slot            TEXT NOT NULL CHECK (active_slot IN ('slot_a', 'slot_b')),
  hardware_fingerprint   TEXT NOT NULL UNIQUE,
  current_channel        TEXT NOT NULL CHECK (current_channel IN ('stable', 'beta')),
  state                  TEXT NOT NULL DEFAULT 'provisioning' CHECK (state IN
                            ('provisioning', 'active', 'draining',
                             'rolled_back', 'decommissioned')),
  last_healthy_boot_at   TEXT,
  arch                   TEXT NOT NULL CHECK (arch IN ('x86_64', 'aarch64'))
);

CREATE INDEX IF NOT EXISTS idx_nodeinst_state ON node_installations (state);

-- =========================================================================
-- tenant_bindings (single-tenant en personal-desktop, pero mismo schema)
-- =========================================================================
CREATE TABLE IF NOT EXISTS tenant_bindings (
  binding_id                       TEXT PRIMARY KEY,
  node_installation_id             TEXT NOT NULL REFERENCES node_installations(node_installation_id),
  tenant_id                        TEXT,
  state                            TEXT NOT NULL DEFAULT 'never_bound' CHECK (state IN
                                       ('never_bound', 'active', 'revoked', 'rebinding')),
  bound_at                         TEXT,
  revoked_at                       TEXT,
  last_rebound_at                  TEXT,
  revocation_cause                 TEXT,
  tenant_provided_endpoint         TEXT,
  tenant_cosign_identity_override  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_binding_per_node
  ON tenant_bindings (node_installation_id) WHERE state = 'active';

-- =========================================================================
-- consents (FR-013 capability-based)
-- =========================================================================
CREATE TABLE IF NOT EXISTS consents (
  consent_id              TEXT PRIMARY KEY,
  node_installation_id    TEXT NOT NULL REFERENCES node_installations(node_installation_id),
  human_user_id           TEXT NOT NULL,
  tenant_id               TEXT,
  capability              TEXT NOT NULL,
  scope_json              TEXT NOT NULL DEFAULT '{}',
  granted_at              TEXT NOT NULL,
  granted_through         TEXT NOT NULL,
  expires_at              TEXT,
  revoked_at              TEXT,
  revoked_reason          TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_consent
  ON consents (node_installation_id, human_user_id, capability)
  WHERE revoked_at IS NULL;

-- =========================================================================
-- ota_update_attempts
-- =========================================================================
CREATE TABLE IF NOT EXISTS ota_update_attempts (
  attempt_id                          TEXT PRIMARY KEY,
  node_installation_id                TEXT NOT NULL REFERENCES node_installations(node_installation_id),
  target_image_version                TEXT NOT NULL,
  target_image_digest                 TEXT NOT NULL,
  from_image_version                  TEXT NOT NULL,
  state                               TEXT NOT NULL DEFAULT 'queued',
  started_at                          TEXT NOT NULL,
  verified_at                         TEXT,
  staged_at                           TEXT,
  promote_attempted_at                TEXT,
  concluded_at                        TEXT,
  rejection_reason                    TEXT,
  rollback_reason                     TEXT,
  runs_paused_count                   INTEGER NOT NULL DEFAULT 0,
  runs_completed_during_drain_count   INTEGER NOT NULL DEFAULT 0,
  training_sessions_persisted_count   INTEGER NOT NULL DEFAULT 0,
  remote_operators_notified_count     INTEGER NOT NULL DEFAULT 0,
  audit_entry_id                      TEXT
);

CREATE INDEX IF NOT EXISTS idx_ota_node_state ON ota_update_attempts (node_installation_id, state);

-- =========================================================================
-- audit_entries (hash-chain local — research §12)
-- =========================================================================
CREATE TABLE IF NOT EXISTS audit_entries (
  entry_id             TEXT PRIMARY KEY,
  node_installation_id TEXT,
  tenant_id            TEXT,
  timestamp            TEXT NOT NULL,
  actor                TEXT NOT NULL,
  audit_kind           TEXT NOT NULL,
  category             TEXT,
  description          TEXT NOT NULL,
  payload_hash         TEXT NOT NULL,
  prev_entry_hash      TEXT NOT NULL,
  signed_payload_hash  TEXT NOT NULL,
  signature_hex        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_node_timestamp
  ON audit_entries (node_installation_id, timestamp);

-- =========================================================================
-- skill_packages (extensión spec 002 — surface_kinds cross-domain)
-- =========================================================================
CREATE TABLE IF NOT EXISTS skill_packages (
  package_id              TEXT PRIMARY KEY,
  tenant_id               TEXT NOT NULL,
  skill_id                TEXT NOT NULL,
  skill_version           INTEGER NOT NULL DEFAULT 1,
  state                   TEXT NOT NULL DEFAULT 'draft',
  signature_hex           TEXT,
  surface_kinds           TEXT NOT NULL DEFAULT '["browser"]',  -- JSON array
  cross_domain            INTEGER NOT NULL DEFAULT 0,           -- bool 0/1
  steps_by_surface_kind   TEXT,                                  -- JSON
  created_at              TEXT NOT NULL
);

-- =========================================================================
-- always_on_policies — FR-040..FR-046 + INVARIANTE screen_lock=false
-- =========================================================================
CREATE TABLE IF NOT EXISTS always_on_policies (
  policy_id                       TEXT PRIMARY KEY,
  node_installation_id            TEXT NOT NULL REFERENCES node_installations(node_installation_id),
  applied_at                      TEXT NOT NULL,
  suspend_targets_masked          INTEGER NOT NULL DEFAULT 1,
  logind_handle_lid_switch        TEXT NOT NULL DEFAULT 'ignore',
  logind_handle_power_key         TEXT NOT NULL DEFAULT 'ignore',
  screen_lock_pauses_agent        INTEGER NOT NULL DEFAULT 0
                                    CHECK (screen_lock_pauses_agent = 0),
  restart_policy_critical         TEXT NOT NULL DEFAULT 'always_with_backoff',
  memory_protection_oom_score_adj INTEGER NOT NULL DEFAULT -500,
  cgroup_memory_min_bytes         INTEGER NOT NULL DEFAULT 536870912,
  drain_required_before_ota       INTEGER NOT NULL DEFAULT 1,
  pending_work_queue_persistent   INTEGER NOT NULL DEFAULT 1,
  agent_activity_indicator_visible INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_aop_node_applied
  ON always_on_policies (node_installation_id, applied_at);

-- =========================================================================
-- first_boot_wizard_sessions
-- =========================================================================
CREATE TABLE IF NOT EXISTS first_boot_wizard_sessions (
  wizard_session_id               TEXT PRIMARY KEY,
  state                           TEXT NOT NULL DEFAULT 'not_started',
  agent_driven                    INTEGER NOT NULL,
  started_at                      TEXT NOT NULL,
  updated_at                      TEXT NOT NULL,
  completed_at                    TEXT,
  abandoned_at                    TEXT,
  collected_profile_kind          TEXT,
  collected_locale_json           TEXT,
  collected_network_decision      TEXT,
  collected_disk_encryption       TEXT,
  collected_tenant_binding_json   TEXT,
  collected_initial_consents_json TEXT,
  reviewed_exposed_services       INTEGER NOT NULL DEFAULT 0,
  collected_channel               TEXT,
  produced_node_installation_id   TEXT REFERENCES node_installations(node_installation_id)
);

-- =========================================================================
-- remote_control_sessions (FR-055 token cifrado at-rest)
-- =========================================================================
CREATE TABLE IF NOT EXISTS remote_control_sessions (
  remote_control_session_id    TEXT PRIMARY KEY,
  node_installation_id         TEXT NOT NULL REFERENCES node_installations(node_installation_id),
  tenant_id                    TEXT NOT NULL,
  operator_id                  TEXT NOT NULL,
  scope                        TEXT NOT NULL CHECK (scope IN
                                  ('os_full_desktop', 'workspace_browser_only')),
  token_ciphertext             BLOB NOT NULL,
  token_kid                    TEXT NOT NULL,
  token_alg                    TEXT NOT NULL DEFAULT 'AES-GCM-256',
  token_expires_at             TEXT NOT NULL,
  dtls_fingerprint             TEXT NOT NULL,
  binding_hash                 TEXT NOT NULL,
  consent_id                   TEXT REFERENCES consents(consent_id),
  state                        TEXT NOT NULL DEFAULT 'issued',
  issued_at                    TEXT NOT NULL,
  accepted_at                  TEXT,
  ended_at                     TEXT,
  end_reason                   TEXT,
  captured_training_steps_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_rcs_tenant_state
  ON remote_control_sessions (tenant_id, state);
