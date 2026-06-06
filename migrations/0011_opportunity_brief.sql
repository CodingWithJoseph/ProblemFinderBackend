-- 0011_opportunity_brief.sql
--
-- Staged opportunity-brief architecture. The single legacy founder brief is
-- replaced by four staged briefs, each with its own JSONB column, API route and
-- server-side prompt:
--
--   brief_discovery  → Signal page           — "is this worth investigating?"
--   brief_market     → Research / Market page — audience, market size, competitors
--   brief_validation → Validate page          — validation questions, experiments
--   brief_build      → Building / Report page  — full product / build spec
--
-- The legacy ``opportunity_brief`` column (the founder brief rendered on the
-- Report and Building pages) maps most closely to the build stage and is
-- renamed to ``brief_build``. Each brief JSONB stores its own ``generated_at``
-- timestamp inside the document; the API treats a brief as stale after 7 days.
--
-- This migration also ensures the pipeline-produced analytics columns the
-- discovery brief reads from exist (they are written by the AI pipeline).
--
-- Idempotent. Run by hand in the Supabase SQL editor — Claude Code cannot apply
-- Supabase migrations.
--
-- Mirrors app/models.py::Cluster.

-- 1. Rename the legacy founder brief into the build stage (guarded so the
--    migration is safe to re-run and safe on a schema where the rename already
--    happened).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'clusters' AND column_name = 'opportunity_brief'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'clusters' AND column_name = 'brief_build'
    ) THEN
        ALTER TABLE clusters RENAME COLUMN opportunity_brief TO brief_build;
    END IF;
END $$;

-- 2. Staged brief columns.
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS brief_build JSONB;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS brief_discovery JSONB;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS brief_market JSONB;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS brief_validation JSONB;

-- 3. Pipeline-produced cluster analytics the discovery brief reads from.
--    (No-ops if the pipeline migration already added them.)
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS top_tfidf_terms JSONB;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS source_breakdown JSONB;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS post_volume_by_date JSONB;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS opportunity_type_counts JSONB;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS opportunity_domain_counts JSONB;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS intra_cluster_density DOUBLE PRECISION;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS silhouette_score DOUBLE PRECISION;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS persistence_score DOUBLE PRECISION;
