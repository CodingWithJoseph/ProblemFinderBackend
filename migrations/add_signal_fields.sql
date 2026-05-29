-- Migration: signal scoring fields, cluster growth metrics, problem embeddings
-- Safe to run multiple times (idempotent).
--
-- Run against the Supabase/Postgres database before deploying the backend and
-- before the AI pipeline writes the new fields.

-- pgvector is required for problems.embedding (already used by opportunities.embedding).
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- opportunities: continuous signal scores + per-post classifier confidences
-- ---------------------------------------------------------------------------
ALTER TABLE opportunities
    ADD COLUMN IF NOT EXISTS intensity_score        float8,
    ADD COLUMN IF NOT EXISTS sentiment_score        float8,
    ADD COLUMN IF NOT EXISTS wtp_confidence         float8,
    ADD COLUMN IF NOT EXISTS workaround_confidence  float8;

-- ---------------------------------------------------------------------------
-- clusters: month-over-month growth + weekly post velocity
-- ---------------------------------------------------------------------------
ALTER TABLE clusters
    ADD COLUMN IF NOT EXISTS growth_rate_mom  float8,
    ADD COLUMN IF NOT EXISTS post_velocity    float8;

-- ---------------------------------------------------------------------------
-- problems: semantic embedding for similar-posts + duplicate detection
-- text-embedding-3-small produces 1536-dimensional vectors.
-- ---------------------------------------------------------------------------
ALTER TABLE problems
    ADD COLUMN IF NOT EXISTS embedding vector(1536);
