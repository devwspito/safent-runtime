-- migration: spec 003 T025 - data-model entidad 11 RemoteControlSession (FR-055).

DO $$ BEGIN
  CREATE TYPE agents_os.remote_control_scope AS ENUM (
    'os_full_desktop', 'workspace_browser_only'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agents_os.remote_control_state AS ENUM (
    'issued', 'active', 'revoked', 'ended'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agents_os.remote_control_end_reason AS ENUM (
    'operator_disconnected', 'user_revoked', 'consent_revoked',
    'tenant_revoked', 'timeout', 'admin_killed', 'service_crash'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS agents_os.remote_control_sessions (
  remote_control_session_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  node_installation_id         uuid NOT NULL REFERENCES agents_os.node_installations(node_installation_id),
  tenant_id                    uuid NOT NULL,
  operator_id                  uuid NOT NULL,
  scope                        agents_os.remote_control_scope NOT NULL,
  -- FR-055: token cifrado at-rest con per-tenant key.
  token_ciphertext             bytea NOT NULL,
  token_kid                    text NOT NULL,
  token_alg                    text NOT NULL DEFAULT 'AES-GCM-256',
  token_expires_at             timestamptz NOT NULL,
  dtls_fingerprint             text NOT NULL,
  -- FR-055: binding adicional (IP+UA+tenant+operator) - HMAC del tuple.
  binding_hash                 text NOT NULL,
  consent_id                   uuid REFERENCES agents_os.consents(consent_id),
  state                        agents_os.remote_control_state NOT NULL DEFAULT 'issued',
  issued_at                    timestamptz NOT NULL DEFAULT now(),
  accepted_at                  timestamptz,
  ended_at                     timestamptz,
  end_reason                   agents_os.remote_control_end_reason,
  captured_training_steps_count int NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_rcs_tenant_state
  ON agents_os.remote_control_sessions (tenant_id, state);
