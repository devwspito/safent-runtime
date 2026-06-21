-- migration: spec 003 T025 - data-model entidad 8 Consent (FR-013, FR-054).

DO $$ BEGIN
  CREATE TYPE agents_os.consent_capability AS ENUM (
    'documents', 'downloads', 'desktop_files', 'camera', 'microphone',
    'network_local', 'package_manager', 'package_manager_admin',
    'system_settings', 'terminal', 'remote_control_subscription',
    'screenshot', 'notifications', 'location', 'filesystem_full'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agents_os.consent_granted_through AS ENUM (
    'xdg_portal_dialog', 'agents_os_consent_dialog',
    'wizard_first_boot', 'panel_settings'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agents_os.consent_revoked_reason AS ENUM (
    'user_revoked', 'tenant_revoked', 'system_policy', 'expired'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS agents_os.consents (
  consent_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  node_installation_id    uuid NOT NULL REFERENCES agents_os.node_installations(node_installation_id),
  human_user_id           uuid NOT NULL,
  tenant_id               uuid,
  capability              agents_os.consent_capability NOT NULL,
  scope_json              jsonb NOT NULL DEFAULT '{}'::jsonb,
  granted_at              timestamptz NOT NULL DEFAULT now(),
  granted_through         agents_os.consent_granted_through NOT NULL,
  expires_at              timestamptz,
  revoked_at              timestamptz,
  revoked_reason          agents_os.consent_revoked_reason
);

-- Un solo consent ACTIVE por (node, user, capability).
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_consent
  ON agents_os.consents (node_installation_id, human_user_id, capability)
  WHERE revoked_at IS NULL;
