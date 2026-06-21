-- migration: spec 003 T026 - audit_entries extension (NO-breaking).

CREATE SCHEMA IF NOT EXISTS kernel;

CREATE TABLE IF NOT EXISTS kernel.audit_entries (
  entry_id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                uuid,
  timestamp                timestamptz NOT NULL DEFAULT now(),
  actor                    text NOT NULL,
  audit_kind               text NOT NULL,
  description              text NOT NULL,
  payload_hash             text NOT NULL,
  prev_entry_hash          text NOT NULL,
  signed_payload_hash      text NOT NULL,
  signature_hex            text NOT NULL
);

-- Aditivo NO-breaking: node_installation_id NULL para registros previos.
ALTER TABLE kernel.audit_entries
  ADD COLUMN IF NOT EXISTS node_installation_id uuid;

-- Aditivo NO-breaking: category para clasificar eventos del SO.
ALTER TABLE kernel.audit_entries
  ADD COLUMN IF NOT EXISTS category text;

CREATE INDEX IF NOT EXISTS idx_audit_node_timestamp
  ON kernel.audit_entries (node_installation_id, timestamp DESC)
  WHERE node_installation_id IS NOT NULL;
