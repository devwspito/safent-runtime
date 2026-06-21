-- migration: spec 003 T025 - data-model entidad 12 SurfaceAdapter registry.

DO $$ BEGIN
  CREATE TYPE agents_os.surface_kind AS ENUM (
    'browser', 'terminal', 'filesystem', 'api_call',
    'desktop_app', 'system_settings', 'package_manager'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS agents_os.surface_adapter_registry (
  registry_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  node_installation_id     uuid NOT NULL REFERENCES agents_os.node_installations(node_installation_id),
  surface_kind             agents_os.surface_kind NOT NULL,
  enabled_for_profiles     text[] NOT NULL,
  capture_method           text NOT NULL,
  replay_method            text NOT NULL,
  landlock_ruleset_ref     text,
  seccomp_allowlist_sig    text,
  consent_capability_required  agents_os.consent_capability,
  UNIQUE (node_installation_id, surface_kind)
);
