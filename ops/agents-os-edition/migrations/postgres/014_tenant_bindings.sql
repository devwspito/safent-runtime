-- migration: spec 003 T025 - data-model entidad 10 TenantBinding (FR-017).

DO $$ BEGIN
  CREATE TYPE agents_os.tenant_binding_state AS ENUM (
    'never_bound', 'active', 'revoked', 'rebinding'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agents_os.tenant_revocation_cause AS ENUM (
    'admin_unbind', 'tenant_admin_revoke_charter', 'system_policy'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS agents_os.tenant_bindings (
  binding_id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  node_installation_id                uuid NOT NULL REFERENCES agents_os.node_installations(node_installation_id),
  tenant_id                           uuid,
  state                               agents_os.tenant_binding_state NOT NULL DEFAULT 'never_bound',
  bound_at                            timestamptz,
  revoked_at                          timestamptz,
  last_rebound_at                     timestamptz,
  revocation_cause                    agents_os.tenant_revocation_cause,
  tenant_provided_endpoint            text,
  tenant_cosign_identity_override     text
);

-- A lo sumo un binding ACTIVE por nodo (FR-017).
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_binding_per_node
  ON agents_os.tenant_bindings (node_installation_id)
  WHERE state = 'active';

CREATE INDEX IF NOT EXISTS idx_tenant_bindings_tenant
  ON agents_os.tenant_bindings (tenant_id);
