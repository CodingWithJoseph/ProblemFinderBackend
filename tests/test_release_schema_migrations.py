from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RETAINED_TABLES = {
    "clusters",
    "cluster_items",
    "search_sessions",
    "search_turns",
    "search_runs",
    "pipeline",
    "signal_analysis_cases",
    "signal_analysis_versions",
    "signal_case_overrides",
    "signal_analysis_jobs",
    "signal_conversations",
    "signal_conversation_turns",
    "problems",
}


def _sql(name: str) -> str:
    return (ROOT / "migrations" / name).read_text(encoding="utf-8").lower()


def test_dead_table_migration_never_drops_release_tables():
    sql = _sql("0049_drop_dead_release_tables.sql")

    for table in RETAINED_TABLES:
        assert f"drop table if exists public.{table} " not in sql


def test_dead_table_migration_covers_removed_feature_families():
    sql = _sql("0049_drop_dead_release_tables.sql")

    for table in {
        "user_flags",
        "subscriptions",
        "teams",
        "issues",
        "tasks",
        "pipeline_stage_events",
        "monitoring_problems",
        "portfolio_customers",
        "cluster_signals",
        "cluster_snapshots",
        "source_posts",
        "opportunities",
        "supabase_clusters",
        "supabase_cluster_items",
    }:
        assert f"drop table if exists public.{table} cascade;" in sql


def test_dead_column_migration_matches_current_ai_publish_contract():
    sql = _sql("0048_drop_dead_release_columns.sql")

    for column in {
        "opportunity_type",
        "opportunity_domain",
        "solution_angle",
        "permalink",
        "scraped_at",
        "distance_to_centroid",
        "membership_confidence",
        "assigned_by",
        "model_version",
    }:
        assert f"drop column if exists {column} cascade" in sql
