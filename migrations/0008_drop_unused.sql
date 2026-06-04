-- 0008_drop_unused.sql
--
-- Drops schema objects confirmed unused across ALL THREE repos (AI pipeline,
-- backend API, web frontend) by the cross-reference audit. Run by hand in the
-- Supabase SQL editor (Claude Code cannot apply Supabase migrations).
--
-- ⚠️  IRREVERSIBLE DATA LOSS. Take a backup / snapshot first and review every
--     line. The matching code changes ship in the same branch:
--       - backend: ORM models, serializers, routers, tests
--       - AI pipeline: writer/trending stop writing the dropped snapshot columns
--       - web: TS types
--
-- Deliberately KEPT despite looking unused (load-bearing — do NOT add here):
--   opportunities.search_vector  -> required by hybrid_search()
--   opportunities.embedding      -> pgvector similarity (similar-posts, coherence)
--   opportunities.source_id      -> upsert dedup key (on_conflict source,source_id)
--   opportunities.model_version  -> written by the pipeline (provenance)
--   opportunities.type / .opportunity_type / .opportunity_domain -> still read/written
--   clusters.centroid            -> cluster identity matching
--   clusters.opportunity_brief/brief_generated_at/brief_model_version -> brief feature
--   clusters.opportunity_types/opportunity_domains -> written by pipeline
--   cluster_snapshots.avg_comments -> read by the pipeline trending detector
--   scrape_state (whole table)   -> pipeline incremental-scrape watermark

BEGIN;

-- 1. Feature-scaffolding tables: full backend routers existed but no data was
--    ever produced and no UI consumed them. (domain_snapshots is KEPT — it is
--    rendered by the dashboard.)
DROP TABLE IF EXISTS experiment_results CASCADE;
DROP TABLE IF EXISTS experiments CASCADE;
DROP TABLE IF EXISTS revenue_signals CASCADE;

-- 2. opportunities — NULL-forever enrichment columns + never-read fields.
ALTER TABLE opportunities
    DROP COLUMN IF EXISTS upvotes,
    DROP COLUMN IF EXISTS noise_filter_keep,
    DROP COLUMN IF EXISTS noise_filter_reason,
    DROP COLUMN IF EXISTS primary_problem,
    DROP COLUMN IF EXISTS target_audience,
    DROP COLUMN IF EXISTS buyer_persona,
    DROP COLUMN IF EXISTS business_model,
    DROP COLUMN IF EXISTS monetization_path,
    DROP COLUMN IF EXISTS estimated_willingness_to_pay,
    DROP COLUMN IF EXISTS competition_level,
    DROP COLUMN IF EXISTS market_saturation,
    DROP COLUMN IF EXISTS revenue_potential_score,
    DROP COLUMN IF EXISTS confidence_score;

-- 3. clusters — backend-exposed but never populated by the pipeline.
ALTER TABLE clusters
    DROP COLUMN IF EXISTS description,
    DROP COLUMN IF EXISTS audience_type,
    DROP COLUMN IF EXISTS problem_category,
    DROP COLUMN IF EXISTS monetization_category,
    DROP COLUMN IF EXISTS revenue_potential_score,
    DROP COLUMN IF EXISTS competition_score,
    DROP COLUMN IF EXISTS saturation_score;

-- 4. cluster_snapshots — NULL-forever analytics + written-but-never-read metrics.
--    (avg_comments is intentionally retained: the trending detector reads it.)
ALTER TABLE cluster_snapshots
    DROP COLUMN IF EXISTS avg_sentiment,
    DROP COLUMN IF EXISTS avg_pain_score,
    DROP COLUMN IF EXISTS avg_urgency_score,
    DROP COLUMN IF EXISTS avg_commercial_intent_score,
    DROP COLUMN IF EXISTS avg_solution_seeking_score,
    DROP COLUMN IF EXISTS signal_score,
    DROP COLUMN IF EXISTS confidence_score,
    DROP COLUMN IF EXISTS top_keywords,
    DROP COLUMN IF EXISTS top_entities,
    DROP COLUMN IF EXISTS revenue_potential_score,
    DROP COLUMN IF EXISTS comment_count,
    DROP COLUMN IF EXISTS unique_author_count,
    DROP COLUMN IF EXISTS unique_community_count,
    DROP COLUMN IF EXISTS avg_score,
    DROP COLUMN IF EXISTS growth_acceleration,
    DROP COLUMN IF EXISTS engagement_velocity;

COMMIT;
