from datetime import datetime, timezone

from sqlalchemy.dialects import postgresql

from app.routes.clusters import _build_cluster_search
from app.search_contract import ClusterSearchQuery


def test_search_contract_normalizes_values_and_dates():
    filters = ClusterSearchQuery(
        query="  billing pain  ",
        sources=[" Reddit ", "reddit", "all", ""],
        observed_after=datetime(2026, 5, 1),
    )

    assert filters.query == "billing pain"
    assert filters.sources == ["Reddit"]
    assert filters.observed_after == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_search_builder_only_compiles_allowlisted_filters():
    filters = ClusterSearchQuery(
        query="billing pain",
        sources=["reddit"],
        communities=["r/freelance"],
        min_posts=3,
        observed_after=datetime(2026, 5, 1, tzinfo=timezone.utc),
        sort="largest",
    )

    base, order_clauses = _build_cluster_search(filters)
    statement = base.order_by(*order_clauses)
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "cluster_items.platform" in sql
    assert "cluster_items.community" in sql
    assert "last_observed" in sql
    assert "cluster_items.opportunity_domain" not in sql
    assert "cluster_signals" not in sql


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
