-- migration: spec-composio T001 — side-table for Composio skills.
--
-- A separate table is cleaner than nullable columns on skill_packages_view:
-- the view schema stays stable and metadata is co-located for the new kind.
--
-- All rows reference skill_packages_view.package_id (TEXT PK).
-- Idempotent: CREATE TABLE IF NOT EXISTS + each ALTER protected against
-- duplicate-column OperationalError by callers.

CREATE TABLE IF NOT EXISTS composio_skills (
  package_id   TEXT PRIMARY KEY,
  toolkit_slug TEXT NOT NULL,
  intent_text  TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
