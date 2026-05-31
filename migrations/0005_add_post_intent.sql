-- 0005_add_post_intent.sql
--
-- Task 2 (scoring): store the per-post intent classification produced by the
-- AI pipeline's GLiClass specialist. One of:
--   venting | seeking | workaround | purchase_intent | cost_complaint
--
-- Nullable text, no backfill required — existing rows stay NULL and are
-- populated on the next pipeline run. Apply in Supabase before deploying the
-- backend ORM change (app/models.py Opportunity.post_intent) and before the
-- next AI run, which writes this column.

ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS post_intent text;
