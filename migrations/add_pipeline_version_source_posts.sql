-- add_pipeline_version_source_posts.sql
--
-- BACKEND JOB 4 — give source_posts the columns the simplified pipeline relies
-- on: a pipeline_version tag (defaulting existing rows to the legacy 'v1') and
-- the noise-filter result. Mirrors the ORM additions in app/models.py
-- (SourcePost.pipeline_version / noise_filter_keep / noise_filter_reason).

ALTER TABLE source_posts
  ADD COLUMN IF NOT EXISTS
  pipeline_version TEXT DEFAULT 'v1';

ALTER TABLE source_posts
  ADD COLUMN IF NOT EXISTS
  noise_filter_keep BOOLEAN;

ALTER TABLE source_posts
  ADD COLUMN IF NOT EXISTS
  noise_filter_reason TEXT;

UPDATE source_posts
SET pipeline_version = 'v1'
WHERE pipeline_version IS NULL;
