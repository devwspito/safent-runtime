-- migration: spec 003 T035 — paridad Postgres con sqlite/002.

BEGIN;

ALTER TABLE agents_os.skill_packages
  ADD COLUMN IF NOT EXISTS intent_caption TEXT NOT NULL DEFAULT '';

ALTER TABLE agents_os.skill_packages
  ADD COLUMN IF NOT EXISTS source_training_session_id UUID;

COMMIT;
