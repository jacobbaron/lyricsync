-- Track when a story (cut) was last changed — any edit op or re-render — so the
-- web "Rendered Cuts" list can show a "last modified" time instead of only
-- created_at. A BEFORE UPDATE trigger bumps it on every row update.
--
-- The /api/projects/[id]/renders route already selects updated_at with a
-- graceful fallback, so it keeps working before and after this migration.

ALTER TABLE stories
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE OR REPLACE FUNCTION set_stories_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS stories_set_updated_at ON stories;
CREATE TRIGGER stories_set_updated_at
  BEFORE UPDATE ON stories
  FOR EACH ROW
  EXECUTE FUNCTION set_stories_updated_at();
