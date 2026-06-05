-- 0010_recreate_user_flags.sql
--
-- Recreate the user_flags table. The flag feature (POST/DELETE
-- /opportunities/{id}/flag) is still active in the backend and the web UI, but
-- the table is absent from the live Supabase schema (migration 0007 was never
-- applied / was dropped). This restores it so the flag endpoints stop 500ing.
--
-- Idempotent (IF NOT EXISTS). Run by hand in the Supabase SQL editor — Claude
-- Code cannot apply Supabase migrations.
--
-- Mirrors app/models.py::UserFlag.

CREATE TABLE IF NOT EXISTS user_flags (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL,
    opportunity_id uuid NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    pipeline_id uuid NULL REFERENCES pipeline(id) ON DELETE SET NULL,
    reason text NOT NULL,
    note text NULL,
    hidden boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_user_flags_user_opportunity UNIQUE (user_id, opportunity_id)
);

CREATE INDEX IF NOT EXISTS idx_user_flags_user_id ON user_flags(user_id);
CREATE INDEX IF NOT EXISTS idx_user_flags_opportunity_id ON user_flags(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_user_flags_pipeline_id ON user_flags(pipeline_id);
