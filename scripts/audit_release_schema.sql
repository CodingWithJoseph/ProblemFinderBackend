-- Read-only Supabase audit for the release-only schema.
--
-- Run in the Supabase SQL editor before migrations 0048 and 0049. The first
-- result classifies public tables. The remaining results expose foreign keys
-- and views that depend on known-dead tables so CASCADE effects can be reviewed.

WITH retained(table_name) AS (
    VALUES
        ('clusters'),
        ('cluster_items'),
        ('search_sessions'),
        ('search_turns'),
        ('search_runs'),
        ('pipeline'),
        ('signal_analysis_cases'),
        ('signal_analysis_versions'),
        ('signal_case_overrides'),
        ('signal_analysis_jobs'),
        ('signal_conversations'),
        ('signal_conversation_turns'),
        ('problems')
),
known_dead(table_name) AS (
    VALUES
        ('user_flags'), ('user_flags_archived'), ('search_usage'),
        ('subscriptions'), ('app_settings'), ('user_preferences'),
        ('user_activity_daily'), ('issue_comments'), ('issues'),
        ('team_members'), ('teams'), ('tasks'), ('pipeline_stage_events'),
        ('project_goal_achievements'), ('account_goal_achievements'),
        ('goal_tiers'), ('goal_definitions'),
        ('monitoring_investigation_entries'), ('monitoring_reports'),
        ('monitoring_investigations'), ('monitoring_problems'),
        ('portfolio_error_events'), ('portfolio_error_groups'),
        ('portfolio_web_vitals'), ('portfolio_logs'), ('portfolio_spans'),
        ('portfolio_usage_events'), ('portfolio_usage_sources'),
        ('portfolio_alert_settings'), ('portfolio_alerts'),
        ('portfolio_identities'), ('portfolio_customers'),
        ('portfolio_revenue_daily'), ('portfolio_mrr_movements'),
        ('portfolio_customer_mrr'), ('portfolio_billing_events'),
        ('portfolio_revenue_sources'), ('cluster_subcluster_members'),
        ('cluster_subclusters'), ('cluster_members'),
        ('cluster_identity_matches'), ('cluster_neighbors'),
        ('cluster_affected_users'), ('cluster_app_readiness'),
        ('cluster_assignment_results'), ('cluster_monthly_signal_counts'),
        ('cluster_naming_summaries'), ('cluster_problem_definitions'),
        ('cluster_problem_momentum'), ('cluster_signals'),
        ('cluster_snapshots'), ('pipeline_runs'), ('domain_snapshots'),
        ('final_opportunities'), ('post_embeddings'), ('opportunities'),
        ('source_posts'), ('scrape_state'), ('cluster_scores'),
        ('model_runs'), ('training_examples'), ('opportunities_brief'),
        ('opportunity_brief')
)
SELECT
    c.relname AS table_name,
    CASE
        WHEN r.table_name IS NOT NULL THEN 'retained'
        WHEN d.table_name IS NOT NULL
          OR c.relname LIKE 'portfolio\_%' ESCAPE '\'
          OR c.relname LIKE 'monitor\_%' ESCAPE '\'
          OR c.relname LIKE 'supabase\_%' ESCAPE '\'
          OR c.relname LIKE 'local_proposed\_%' ESCAPE '\'
        THEN 'known_dead'
        ELSE 'unclassified_review_required'
    END AS release_classification,
    c.reltuples::bigint AS estimated_rows,
    pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN retained r ON r.table_name = c.relname
LEFT JOIN known_dead d ON d.table_name = c.relname
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p')
  AND NOT EXISTS (
      SELECT 1
      FROM pg_depend dep
      WHERE dep.classid = 'pg_class'::regclass
        AND dep.objid = c.oid
        AND dep.deptype = 'e'
  )
ORDER BY release_classification, c.relname;

WITH dead_prefixes AS (
    SELECT c.oid, c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND (
          c.relname LIKE 'portfolio\_%' ESCAPE '\'
          OR c.relname LIKE 'monitor\_%' ESCAPE '\'
          OR c.relname LIKE 'monitoring\_%' ESCAPE '\'
          OR c.relname LIKE 'supabase\_%' ESCAPE '\'
          OR c.relname LIKE 'local_proposed\_%' ESCAPE '\'
          OR c.relname IN (
              'user_flags', 'user_flags_archived', 'search_usage',
              'subscriptions', 'app_settings', 'user_preferences',
              'user_activity_daily', 'issue_comments', 'issues',
              'team_members', 'teams', 'tasks', 'pipeline_stage_events',
              'project_goal_achievements', 'account_goal_achievements',
              'goal_tiers', 'goal_definitions', 'cluster_subcluster_members',
              'cluster_subclusters', 'cluster_members',
              'cluster_identity_matches', 'cluster_neighbors',
              'cluster_affected_users', 'cluster_app_readiness',
              'cluster_assignment_results', 'cluster_monthly_signal_counts',
              'cluster_naming_summaries', 'cluster_problem_definitions',
              'cluster_problem_momentum', 'cluster_signals',
              'cluster_snapshots', 'pipeline_runs', 'domain_snapshots',
              'final_opportunities', 'post_embeddings', 'opportunities',
              'source_posts', 'scrape_state', 'cluster_scores',
              'model_runs', 'training_examples', 'opportunities_brief',
              'opportunity_brief'
          )
      )
)
SELECT
    con.conname AS foreign_key,
    con.conrelid::regclass AS referencing_table,
    con.confrelid::regclass AS referenced_dead_table
FROM pg_constraint con
JOIN dead_prefixes d ON d.oid = con.confrelid
WHERE con.contype = 'f'
ORDER BY con.confrelid::regclass::text, con.conrelid::regclass::text, con.conname;

WITH dead_relations AS (
    SELECT c.oid
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND (
          c.relname LIKE 'portfolio\_%' ESCAPE '\'
          OR c.relname LIKE 'monitor\_%' ESCAPE '\'
          OR c.relname LIKE 'monitoring\_%' ESCAPE '\'
          OR c.relname LIKE 'supabase\_%' ESCAPE '\'
          OR c.relname LIKE 'local_proposed\_%' ESCAPE '\'
          OR c.relname IN (
              'cluster_signals', 'cluster_snapshots', 'opportunities',
              'source_posts', 'cluster_members', 'teams', 'issues', 'tasks'
          )
      )
)
SELECT DISTINCT
    dependent_ns.nspname AS dependent_schema,
    dependent.relname AS dependent_view
FROM pg_depend dep
JOIN dead_relations dead ON dead.oid = dep.refobjid
JOIN pg_rewrite rewrite ON rewrite.oid = dep.objid
JOIN pg_class dependent ON dependent.oid = rewrite.ev_class
JOIN pg_namespace dependent_ns ON dependent_ns.oid = dependent.relnamespace
WHERE dependent.relkind IN ('v', 'm')
ORDER BY dependent_schema, dependent_view;
