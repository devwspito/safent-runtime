-- migration: spec 003 T025 - data-model entidad 4 NodeInstallation
-- NO-breaking con spec 002 (constitución I). Idempotente.

CREATE SCHEMA IF NOT EXISTS agents_os;

DO $$ BEGIN
  CREATE TYPE agents_os.profile_kind AS ENUM ('workspace_only', 'personal_desktop', 'server');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agents_os.node_state AS ENUM (
    'provisioning', 'active', 'draining', 'rolled_back', 'decommissioned'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agents_os.operational_model AS ENUM (
    'cloud_saas_managed', 'self_hosted'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS agents_os.node_installations (
  node_installation_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  installed_at            timestamptz NOT NULL DEFAULT now(),
  profile_kind            agents_os.profile_kind NOT NULL,
  operational_model       agents_os.operational_model NOT NULL,
  current_image_version   text NOT NULL,
  previous_image_version  text,
  active_slot             text NOT NULL CHECK (active_slot IN ('slot_a', 'slot_b')),
  hardware_fingerprint    text NOT NULL UNIQUE,
  current_channel         text NOT NULL CHECK (current_channel IN ('stable', 'beta')),
  state                   agents_os.node_state NOT NULL DEFAULT 'provisioning',
  last_healthy_boot_at    timestamptz,
  arch                    text NOT NULL CHECK (arch IN ('x86_64', 'aarch64'))
);

CREATE INDEX IF NOT EXISTS idx_nodeinst_state
  ON agents_os.node_installations (state);
