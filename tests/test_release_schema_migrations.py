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
    "solution_cases",
    "solution_versions",
    "solution_jobs",
    "ide_access_tokens",
    "solution_task_states",
    "problems",
}


def test_solution_warroom_migration_is_versioned_and_traceable():
    sql = _sql("0050_solution_warroom.sql")

    for table in {"solution_cases", "solution_versions", "solution_jobs"}:
        assert f"create table if not exists {table}" in sql
    assert "problem_case_version integer not null" in sql
    assert "unique (case_id, version)" in sql


def test_ide_mcp_migration_uses_hashed_revocable_project_tokens():
    sql = _sql("0051_ide_mcp_access.sql")

    assert "create table if not exists ide_access_tokens" in sql
    assert "token_hash varchar(64) not null unique" in sql
    assert "revoked_at timestamptz null" in sql
    assert "create table if not exists solution_task_states" in sql
    assert "unique (case_id, task_id)" in sql


def test_build_contract_migration_pins_approval_and_verification():
    sql = _sql("0052_build_contract_foundation.sql")

    assert "approved_version_id uuid" in sql
    assert "approval_status varchar(24)" in sql
    assert "verification jsonb" in sql


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
