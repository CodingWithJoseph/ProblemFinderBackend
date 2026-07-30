-- 0048_drop_dead_release_columns.sql
--
-- Remove columns that belonged to retired product features or to superseded
-- ProblemFinderAI publish contracts. Run this before 0049 so foreign keys from
-- retained tables (notably pipeline.team_id) are detached before their target
-- tables are removed.
--
-- Destructive and intentionally forward-only. Take a Supabase backup before
-- applying. IF EXISTS keeps the migration safe across partially migrated
-- environments; CASCADE removes views or constraints that still depend on a
-- dead column.

BEGIN;

-- Pipeline keeps only the released project identity and lifecycle fields:
-- id, user_id, name, project_name, post_ids, source_cluster_id, stage,
-- launched_at, removed_at, created_at, updated_at.
ALTER TABLE IF EXISTS public.pipeline
    DROP COLUMN IF EXISTS team_id CASCADE,
    DROP COLUMN IF EXISTS icon_url CASCADE,
    DROP COLUMN IF EXISTS kill_criteria CASCADE,
    DROP COLUMN IF EXISTS distribution_channels CASCADE,
    DROP COLUMN IF EXISTS concept_angles CASCADE,
    DROP COLUMN IF EXISTS exit_checklist CASCADE,
    DROP COLUMN IF EXISTS notes CASCADE,
    DROP COLUMN IF EXISTS url CASCADE,
    DROP COLUMN IF EXISTS category CASCADE,
    DROP COLUMN IF EXISTS revenue_model CASCADE,
    DROP COLUMN IF EXISTS status CASCADE,
    DROP COLUMN IF EXISTS cluster_metrics CASCADE,
    DROP COLUMN IF EXISTS outcome CASCADE,
    DROP COLUMN IF EXISTS mrr CASCADE,
    DROP COLUMN IF EXISTS outcome_noted_at CASCADE,
    DROP COLUMN IF EXISTS timeline_days CASCADE,
    DROP COLUMN IF EXISTS timeline_start CASCADE,
    DROP COLUMN IF EXISTS timeline_target_launch CASCADE;

-- The v2 publisher writes only id, name, summary and pipeline_version to the
-- public clusters table. created_at/updated_at remain database bookkeeping.
ALTER TABLE IF EXISTS public.clusters
    DROP COLUMN IF EXISTS audience_type CASCADE,
    DROP COLUMN IF EXISTS author_count CASCADE,
    DROP COLUMN IF EXISTS avg_commercial_intent_score CASCADE,
    DROP COLUMN IF EXISTS avg_distance_to_centroid CASCADE,
    DROP COLUMN IF EXISTS avg_pain_score CASCADE,
    DROP COLUMN IF EXISTS avg_solution_seeking_score CASCADE,
    DROP COLUMN IF EXISTS avg_urgency_score CASCADE,
    DROP COLUMN IF EXISTS brief_build CASCADE,
    DROP COLUMN IF EXISTS brief_discovery CASCADE,
    DROP COLUMN IF EXISTS brief_generated_at CASCADE,
    DROP COLUMN IF EXISTS brief_market CASCADE,
    DROP COLUMN IF EXISTS brief_model_version CASCADE,
    DROP COLUMN IF EXISTS brief_validation CASCADE,
    DROP COLUMN IF EXISTS centroid CASCADE,
    DROP COLUMN IF EXISTS community_count CASCADE,
    DROP COLUMN IF EXISTS competition_score CASCADE,
    DROP COLUMN IF EXISTS confidence_score CASCADE,
    DROP COLUMN IF EXISTS description CASCADE,
    DROP COLUMN IF EXISTS first_seen CASCADE,
    DROP COLUMN IF EXISTS growth_acceleration CASCADE,
    DROP COLUMN IF EXISTS growth_rate CASCADE,
    DROP COLUMN IF EXISTS growth_rate_mom CASCADE,
    DROP COLUMN IF EXISTS growth_rate_wow CASCADE,
    DROP COLUMN IF EXISTS intra_cluster_density CASCADE,
    DROP COLUMN IF EXISTS is_new CASCADE,
    DROP COLUMN IF EXISTS is_singleton CASCADE,
    DROP COLUMN IF EXISTS last_seen_date CASCADE,
    DROP COLUMN IF EXISTS monetization_category CASCADE,
    DROP COLUMN IF EXISTS opportunity_brief CASCADE,
    DROP COLUMN IF EXISTS opportunity_domain CASCADE,
    DROP COLUMN IF EXISTS opportunity_domain_counts CASCADE,
    DROP COLUMN IF EXISTS opportunity_domains CASCADE,
    DROP COLUMN IF EXISTS opportunity_score CASCADE,
    DROP COLUMN IF EXISTS opportunity_type CASCADE,
    DROP COLUMN IF EXISTS opportunity_type_counts CASCADE,
    DROP COLUMN IF EXISTS opportunity_types CASCADE,
    DROP COLUMN IF EXISTS overview_error CASCADE,
    DROP COLUMN IF EXISTS overview_generated_at CASCADE,
    DROP COLUMN IF EXISTS overview_input_fingerprint CASCADE,
    DROP COLUMN IF EXISTS overview_model_version CASCADE,
    DROP COLUMN IF EXISTS overview_schema_version CASCADE,
    DROP COLUMN IF EXISTS overview_status CASCADE,
    DROP COLUMN IF EXISTS persistence_score CASCADE,
    DROP COLUMN IF EXISTS pipeline_cluster_id CASCADE,
    DROP COLUMN IF EXISTS platform CASCADE,
    DROP COLUMN IF EXISTS post_count CASCADE,
    DROP COLUMN IF EXISTS post_velocity CASCADE,
    DROP COLUMN IF EXISTS post_volume_by_date CASCADE,
    DROP COLUMN IF EXISTS primary_domain CASCADE,
    DROP COLUMN IF EXISTS problem_category CASCADE,
    DROP COLUMN IF EXISTS revenue_potential_score CASCADE,
    DROP COLUMN IF EXISTS saturation_score CASCADE,
    DROP COLUMN IF EXISTS signal_overview CASCADE,
    DROP COLUMN IF EXISTS signal_score CASCADE,
    DROP COLUMN IF EXISTS silhouette_score CASCADE,
    DROP COLUMN IF EXISTS source CASCADE,
    DROP COLUMN IF EXISTS source_breakdown CASCADE,
    DROP COLUMN IF EXISTS status CASCADE,
    DROP COLUMN IF EXISTS subreddits CASCADE,
    DROP COLUMN IF EXISTS top_tfidf_terms CASCADE,
    DROP COLUMN IF EXISTS trending CASCADE,
    DROP COLUMN IF EXISTS trending_status CASCADE;

-- The v2 public item payload keeps:
-- id, cluster_id, pipeline_version, platform, community, source_item_id, title,
-- body, url, author, score, num_comments, posted_at, raw_json,
-- problem_statement, similarity_score and embedding.
ALTER TABLE IF EXISTS public.cluster_items
    DROP COLUMN IF EXISTS permalink CASCADE,
    DROP COLUMN IF EXISTS top_comments CASCADE,
    DROP COLUMN IF EXISTS upvote_ratio CASCADE,
    DROP COLUMN IF EXISTS scraped_at CASCADE,
    DROP COLUMN IF EXISTS content_hash CASCADE,
    DROP COLUMN IF EXISTS opportunity_type CASCADE,
    DROP COLUMN IF EXISTS opportunity_domain CASCADE,
    DROP COLUMN IF EXISTS solution_angle CASCADE,
    DROP COLUMN IF EXISTS distance_to_centroid CASCADE,
    DROP COLUMN IF EXISTS membership_confidence CASCADE,
    DROP COLUMN IF EXISTS assigned_by CASCADE,
    DROP COLUMN IF EXISTS model_version CASCADE,
    DROP COLUMN IF EXISTS rejection_reason CASCADE,
    DROP COLUMN IF EXISTS status CASCADE;

COMMIT;
