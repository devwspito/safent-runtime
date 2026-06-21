-- migration: spec 003 T025 - data-model entidad 6 OtaUpdateAttempt.

DO $$ BEGIN
  CREATE TYPE agents_os.ota_state AS ENUM (
    'queued', 'downloading', 'verifying', 'drain_in_progress',
    'staged', 'booting_target', 'promoted', 'rolled_back', 'rejected', 'aborted'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agents_os.ota_rejection_reason AS ENUM (
    'signature_invalid', 'size_budget_exceeded', 'sbom_missing', 'sbom_mismatch',
    'disk_full', 'network_error', 'clock_skew_severe', 'image_revoked',
    'profile_not_supported', 'downgrade_blocked'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agents_os.ota_rollback_reason AS ENUM (
    'healthy_target_timeout', 'kernel_panic', 'critical_service_failed', 'manual_admin'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS agents_os.ota_update_attempts (
  attempt_id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  node_installation_id                    uuid NOT NULL REFERENCES agents_os.node_installations(node_installation_id),
  target_image_version                    text NOT NULL,
  target_image_digest                     text NOT NULL,
  from_image_version                      text NOT NULL,
  state                                   agents_os.ota_state NOT NULL DEFAULT 'queued',
  started_at                              timestamptz NOT NULL DEFAULT now(),
  verified_at                             timestamptz,
  staged_at                               timestamptz,
  promote_attempted_at                    timestamptz,
  concluded_at                            timestamptz,
  rejection_reason                        agents_os.ota_rejection_reason,
  rollback_reason                         agents_os.ota_rollback_reason,
  runs_paused_count                       int NOT NULL DEFAULT 0,
  runs_completed_during_drain_count       int NOT NULL DEFAULT 0,
  training_sessions_persisted_count       int NOT NULL DEFAULT 0,
  remote_operators_notified_count         int NOT NULL DEFAULT 0,
  audit_entry_id                          uuid
);

CREATE INDEX IF NOT EXISTS idx_ota_node_state
  ON agents_os.ota_update_attempts (node_installation_id, state);
