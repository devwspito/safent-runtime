-- migration: spec 003 T035 — completa el schema skill_packages para
-- preservar la integridad del SkillCompiler.verify() tras round-trip.

ALTER TABLE skill_packages ADD COLUMN intent_caption TEXT NOT NULL DEFAULT '';
ALTER TABLE skill_packages ADD COLUMN source_training_session_id TEXT;
