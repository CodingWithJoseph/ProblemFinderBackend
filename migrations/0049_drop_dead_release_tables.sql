-- 0049_drop_dead_release_tables.sql
--
-- Hard-delete tables owned exclusively by removed features and superseded AI
-- schemas. Apply after 0048. CASCADE is deliberate: policies, triggers, views,
-- foreign keys and SQL functions that still depend on a dead table are dead too.
--
-- Destructive and intentionally forward-only. Take a Supabase backup first.

BEGIN;

-- Removed account, billing and feature-gating state.
DROP TABLE IF EXISTS public.user_flags CASCADE;
DROP TABLE IF EXISTS public.user_flags_archived CASCADE;
DROP TABLE IF EXISTS public.search_usage CASCADE;
DROP TABLE IF EXISTS public.subscriptions CASCADE;
DROP TABLE IF EXISTS public.app_settings CASCADE;
DROP TABLE IF EXISTS public.user_preferences CASCADE;
DROP TABLE IF EXISTS public.user_activity_daily CASCADE;

-- Removed collaboration, issue and task surfaces.
DROP TABLE IF EXISTS public.issue_comments CASCADE;
DROP TABLE IF EXISTS public.issues CASCADE;
DROP TABLE IF EXISTS public.team_members CASCADE;
DROP TABLE IF EXISTS public.teams CASCADE;
DROP TABLE IF EXISTS public.tasks CASCADE;

-- Removed timeline and goals surfaces.
DROP TABLE IF EXISTS public.pipeline_stage_events CASCADE;
DROP TABLE IF EXISTS public.project_goal_achievements CASCADE;
DROP TABLE IF EXISTS public.account_goal_achievements CASCADE;
DROP TABLE IF EXISTS public.goal_tiers CASCADE;
DROP TABLE IF EXISTS public.goal_definitions CASCADE;

-- Removed Monitor/Portfolio storage: deployed physical names.
DROP TABLE IF EXISTS public.monitoring_investigation_entries CASCADE;
DROP TABLE IF EXISTS public.monitoring_reports CASCADE;
DROP TABLE IF EXISTS public.monitoring_investigations CASCADE;
DROP TABLE IF EXISTS public.monitoring_problems CASCADE;
DROP TABLE IF EXISTS public.portfolio_error_events CASCADE;
DROP TABLE IF EXISTS public.portfolio_error_groups CASCADE;
DROP TABLE IF EXISTS public.portfolio_web_vitals CASCADE;
DROP TABLE IF EXISTS public.portfolio_logs CASCADE;
DROP TABLE IF EXISTS public.portfolio_spans CASCADE;
DROP TABLE IF EXISTS public.portfolio_usage_events CASCADE;
DROP TABLE IF EXISTS public.portfolio_usage_sources CASCADE;
DROP TABLE IF EXISTS public.portfolio_alert_settings CASCADE;
DROP TABLE IF EXISTS public.portfolio_alerts CASCADE;
DROP TABLE IF EXISTS public.portfolio_identities CASCADE;
DROP TABLE IF EXISTS public.portfolio_customers CASCADE;
DROP TABLE IF EXISTS public.portfolio_revenue_daily CASCADE;
DROP TABLE IF EXISTS public.portfolio_mrr_movements CASCADE;
DROP TABLE IF EXISTS public.portfolio_customer_mrr CASCADE;
DROP TABLE IF EXISTS public.portfolio_billing_events CASCADE;
DROP TABLE IF EXISTS public.portfolio_revenue_sources CASCADE;

-- Removed Monitor canonical/compatibility names. These were recorded during a
-- naming transition even when a given environment never created them.
DROP TABLE IF EXISTS public.monitor_investigation_entries CASCADE;
DROP TABLE IF EXISTS public.monitor_reports CASCADE;
DROP TABLE IF EXISTS public.monitor_investigations CASCADE;
DROP TABLE IF EXISTS public.monitor_problems CASCADE;
DROP TABLE IF EXISTS public.monitor_error_events CASCADE;
DROP TABLE IF EXISTS public.monitor_error_groups CASCADE;
DROP TABLE IF EXISTS public.monitor_web_vitals CASCADE;
DROP TABLE IF EXISTS public.monitor_logs CASCADE;
DROP TABLE IF EXISTS public.monitor_spans CASCADE;
DROP TABLE IF EXISTS public.monitor_usage_events CASCADE;
DROP TABLE IF EXISTS public.monitor_usage_sources CASCADE;
DROP TABLE IF EXISTS public.monitor_alert_settings CASCADE;
DROP TABLE IF EXISTS public.monitor_alerts CASCADE;

-- Superseded AI discovery schemas. cluster_items replaced the source/member/
-- opportunity triad; on-demand Signal analysis replaced snapshot/signal state.
DROP TABLE IF EXISTS public.cluster_subcluster_members CASCADE;
DROP TABLE IF EXISTS public.cluster_subclusters CASCADE;
DROP TABLE IF EXISTS public.cluster_members CASCADE;
DROP TABLE IF EXISTS public.cluster_identity_matches CASCADE;
DROP TABLE IF EXISTS public.cluster_neighbors CASCADE;
DROP TABLE IF EXISTS public.cluster_affected_users CASCADE;
DROP TABLE IF EXISTS public.cluster_app_readiness CASCADE;
DROP TABLE IF EXISTS public.cluster_assignment_results CASCADE;
DROP TABLE IF EXISTS public.cluster_monthly_signal_counts CASCADE;
DROP TABLE IF EXISTS public.cluster_naming_summaries CASCADE;
DROP TABLE IF EXISTS public.cluster_problem_definitions CASCADE;
DROP TABLE IF EXISTS public.cluster_problem_momentum CASCADE;
DROP TABLE IF EXISTS public.cluster_signals CASCADE;
DROP TABLE IF EXISTS public.cluster_snapshots CASCADE;
DROP TABLE IF EXISTS public.pipeline_runs CASCADE;
DROP TABLE IF EXISTS public.domain_snapshots CASCADE;
DROP TABLE IF EXISTS public.final_opportunities CASCADE;
DROP TABLE IF EXISTS public.post_embeddings CASCADE;
DROP TABLE IF EXISTS public.opportunities CASCADE;
DROP TABLE IF EXISTS public.source_posts CASCADE;
DROP TABLE IF EXISTS public.scrape_state CASCADE;
DROP TABLE IF EXISTS public.cluster_scores CASCADE;
DROP TABLE IF EXISTS public.model_runs CASCADE;
DROP TABLE IF EXISTS public.training_examples CASCADE;
DROP TABLE IF EXISTS public.opportunities_brief CASCADE;
DROP TABLE IF EXISTS public.opportunity_brief CASCADE;

-- Old local-to-Supabase mirror tables were never product state. Drop them if a
-- shared environment accidentally received a local migration.
DROP TABLE IF EXISTS public.local_proposed_cluster_members CASCADE;
DROP TABLE IF EXISTS public.local_proposed_clusters CASCADE;
DROP TABLE IF EXISTS public.supabase_cluster_items CASCADE;
DROP TABLE IF EXISTS public.supabase_clusters CASCADE;
DROP TABLE IF EXISTS public.supabase_cluster_identity_matches CASCADE;
DROP TABLE IF EXISTS public.supabase_cluster_members CASCADE;
DROP TABLE IF EXISTS public.supabase_cluster_neighbors CASCADE;
DROP TABLE IF EXISTS public.supabase_cluster_signals CASCADE;
DROP TABLE IF EXISTS public.supabase_cluster_snapshots CASCADE;
DROP TABLE IF EXISTS public.supabase_cluster_subcluster_members CASCADE;
DROP TABLE IF EXISTS public.supabase_cluster_subclusters CASCADE;
DROP TABLE IF EXISTS public.supabase_domain_snapshots CASCADE;
DROP TABLE IF EXISTS public.supabase_opportunities CASCADE;
DROP TABLE IF EXISTS public.supabase_pipeline_runs CASCADE;
DROP TABLE IF EXISTS public.supabase_source_posts CASCADE;

DROP TYPE IF EXISTS public.cluster_signal_status CASCADE;
DROP TYPE IF EXISTS public.planenum CASCADE;

COMMIT;
