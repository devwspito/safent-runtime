-- migration: spec 003 T025 - data-model entidad 3 FirstBootWizard.

DO $$ BEGIN
  CREATE TYPE agents_os.wizard_state AS ENUM (
    'not_started', 'collecting_profile', 'collecting_locale',
    'collecting_network', 'collecting_tenant_binding',
    'collecting_consents', 'reviewing_exposed_services',
    'finalizing', 'completed', 'abandoned', 'fallback_traditional_ui'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS agents_os.first_boot_wizard_sessions (
  wizard_session_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  state                           agents_os.wizard_state NOT NULL DEFAULT 'not_started',
  agent_driven                    boolean NOT NULL,
  started_at                      timestamptz NOT NULL DEFAULT now(),
  updated_at                      timestamptz NOT NULL DEFAULT now(),
  completed_at                    timestamptz,
  abandoned_at                    timestamptz,
  collected_profile_kind          agents_os.profile_kind,
  collected_locale_json           jsonb,
  collected_network_decision      text,
  collected_disk_encryption       text,
  collected_tenant_binding_json   jsonb,
  collected_initial_consents_json jsonb,
  reviewed_exposed_services       boolean NOT NULL DEFAULT false,
  collected_channel               text,
  produced_node_installation_id   uuid REFERENCES agents_os.node_installations(node_installation_id)
);
