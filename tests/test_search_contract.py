from datetime import datetime, timezone

from sqlalchemy.dialects import postgresql

from app.routes.clusters import _build_cluster_search
from app.search_contract import ClusterSearchQuery


def test_search_contract_normalizes_values_and_dates():
    filters = ClusterSearchQuery(
        query="  billing pain  ",
        opportunity_domains=[" Fintech ", "fintech", "all", ""],
        observed_after=datetime(2026, 5, 1),
    )

    assert filters.query == "billing pain"
    assert filters.opportunity_domains == ["Fintech"]
    assert filters.observed_after == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_search_builder_only_compiles_allowlisted_filters():
    filters = ClusterSearchQuery(
        query="billing pain",
        opportunity_domains=["fintech"],
        opportunity_types=["software"],
        sources=["reddit"],
        communities=["r/freelance"],
        min_posts=3,
        observed_after=datetime(2026, 5, 1, tzinfo=timezone.utc),
        trending_only=True,
        min_signal_score=0.7,
        sort="signal_score",
    )

    base, order_clauses = _build_cluster_search(filters)
    statement = base.order_by(*order_clauses)
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "cluster_items.opportunity_domain" in sql
    assert "cluster_items.opportunity_type" in sql
    assert "cluster_items.platform" in sql
    assert "cluster_items.community" in sql
    assert "cluster_signals" in sql
    assert "last_observed" in sql
    assert "clusters.trending IS true" in sql


def test_search_builder_drops_conversational_stop_words_from_text_matching():
    base, order_clauses = _build_cluster_search(
        ClusterSearchQuery(query="show me problems in healthcare")
    )
    compiled = base.order_by(*order_clauses).compile(dialect=postgresql.dialect())
    patterns = {
        value.casefold()
        for value in compiled.params.values()
        if isinstance(value, str) and value.startswith("%")
    }

    assert "%healthcare%" in patterns
    assert "%problems%" not in patterns
    assert "%in%" not in patterns
