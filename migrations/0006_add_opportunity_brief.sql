-- 0006_add_opportunity_brief.sql
--
-- Task 3 (Opportunity Brief): store the auto-generated, founder-ready brief for
-- high-signal clusters. The AI pipeline generates it at classification time for
-- clusters with signal_score >= 50; Pro+ users can regenerate it on demand.
--
--   opportunity_brief      jsonb        -- the structured OpportunityBrief JSON
--   brief_generated_at     timestamptz  -- when it was last (re)generated
--   brief_model_version    text         -- the Claude model that produced it
--
-- All nullable, no backfill — existing clusters stay NULL until the next run or
-- a manual refresh. Apply in Supabase before deploying the backend ORM change
-- (app/models.py Cluster) and before the next AI run.

ALTER TABLE clusters ADD COLUMN IF NOT EXISTS opportunity_brief jsonb;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS brief_generated_at timestamptz;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS brief_model_version text;
