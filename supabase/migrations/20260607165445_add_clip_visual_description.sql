-- Coarse visual context note per clip, produced by the VIS-01 "context"
-- variant: a cheap 1-3 sentence description of what happens on screen, meant
-- to be referenced alongside the transcript by downstream story generation.
ALTER TABLE clips ADD COLUMN IF NOT EXISTS visual_description text;

COMMENT ON COLUMN clips.visual_description IS 'Coarse one-to-three sentence visual context note (from the VIS-01 "context" variant) to accompany the transcript.';
