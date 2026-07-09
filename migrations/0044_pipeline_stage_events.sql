-- 0044_pipeline_stage_events.sql
--
-- Append-only log of a project entering a pipeline stage, so the Timeline can
-- decompose the pre-launch journey (watching -> exploring -> validating ->
-- building) into real durations instead of a fixed 30/70 approximation.
--
-- Written on project creation (the initial "watching" event) and whenever the
-- stage changes via PATCH /pipeline/{id}. Launch is tracked separately by
-- pipeline.launched_at, so there is no "launched" stage row.
--
-- Additive and idempotent (IF NOT EXISTS + a guarded backfill).

CREATE TABLE IF NOT EXISTS pipeline_stage_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_id UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    entered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pipeline_stage_events_pipeline ON pipeline_stage_events(pipeline_id, entered_at);

-- Backfill: seed each existing (non-removed) project's current stage at its
-- creation time so the timeline has a starting point. Guarded so re-applying the
-- migration never double-seeds.
INSERT INTO pipeline_stage_events (pipeline_id, stage, entered_at)
SELECT p.id, p.stage, p.created_at
FROM pipeline p
WHERE p.removed_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM pipeline_stage_events e WHERE e.pipeline_id = p.id);
