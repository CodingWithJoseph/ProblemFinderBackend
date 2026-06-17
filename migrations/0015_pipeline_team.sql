-- 0015_pipeline_team.sql
--
-- MVP project ownership link: a pipeline card is the internal Project record,
-- and a project can be assigned to one collaboration team.

ALTER TABLE pipeline
    ADD COLUMN IF NOT EXISTS team_id UUID REFERENCES teams(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_pipeline_team_id ON pipeline(team_id);
