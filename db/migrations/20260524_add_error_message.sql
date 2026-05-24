-- Applied 2026-05-24 via Supabase MCP
ALTER TABLE clips    ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE stories  ADD COLUMN IF NOT EXISTS error_message TEXT;
