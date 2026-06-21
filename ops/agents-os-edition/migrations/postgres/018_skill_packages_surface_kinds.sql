-- migration: spec 003 T026 - FR-029 SkillCrossDomain (aditivo NO-breaking).
-- Asume que la tabla kernel.skill_packages existe (spec 002).
-- Si no existe (BD limpia), crea stub mínimo + ALTER.

CREATE SCHEMA IF NOT EXISTS kernel;

CREATE TABLE IF NOT EXISTS kernel.skill_packages (
  package_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id               uuid NOT NULL,
  skill_id                uuid NOT NULL,
  skill_version           int NOT NULL DEFAULT 1,
  state                   text NOT NULL DEFAULT 'draft',
  signature_hex           text,
  created_at              timestamptz NOT NULL DEFAULT now()
);

-- Aditivo NO-breaking (constitución I): default browser para skills existentes.
ALTER TABLE kernel.skill_packages
  ADD COLUMN IF NOT EXISTS surface_kinds text[] NOT NULL DEFAULT '{browser}';

ALTER TABLE kernel.skill_packages
  ADD COLUMN IF NOT EXISTS cross_domain boolean NOT NULL DEFAULT false;

ALTER TABLE kernel.skill_packages
  ADD COLUMN IF NOT EXISTS steps_by_surface_kind jsonb;

-- Mantener cross_domain coherente con surface_kinds.
CREATE OR REPLACE FUNCTION agents_os.set_cross_domain()
RETURNS trigger AS $$
BEGIN
  NEW.cross_domain := array_length(NEW.surface_kinds, 1) > 1;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_skill_packages_cross_domain
  ON kernel.skill_packages;
CREATE TRIGGER trg_skill_packages_cross_domain
  BEFORE INSERT OR UPDATE OF surface_kinds
  ON kernel.skill_packages
  FOR EACH ROW EXECUTE FUNCTION agents_os.set_cross_domain();
