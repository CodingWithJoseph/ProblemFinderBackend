from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PIPELINE_VERSION
from app.discovery_contract import (
    DiscoveryCardResponse,
    DiscoveryCategory, DiscoveryDashboardResponse, DiscoveryCollectionsResponse,
)
from app.models import ClusterItem, Cluster
from app.services.cluster_membership import load_watched_cluster_ids

DISCOVERY_CATEGORY = (
    DiscoveryCategory.BUILD_AND_TOOLING,
    DiscoveryCategory.DATA_AND_APIS,
    DiscoveryCategory.WORKFLOW_AND_AUTOMATION
)

DISCOVERY_COLLECTION_LIMIT = 6


def _has_text(column):
    return func.regexp_replace(column, r"\s", "", "g") != ""


def build_discovery_query():
    has_problem_statement = (
        select(ClusterItem.id)
        .where(
            ClusterItem.cluster_id == Cluster.id,
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            _has_text(ClusterItem.problem_statement)
        ).exists()
    )

    stats = _items_stats_discovery()
    problem_statement = _problem_statement_discovery()

    return (select(
        Cluster,
        stats.c.post_count,
        stats.c.last_observed,
        problem_statement.label("problem_statement"),
    ).join(stats, stats.c.cluster_id == Cluster.id)
    .where(
        Cluster.pipeline_version == PIPELINE_VERSION,
        _has_text(Cluster.name),
        _has_text(Cluster.summary),
        has_problem_statement
    ))


def _items_stats_discovery():
    return (
        select(
            ClusterItem.cluster_id.label("cluster_id"),
            func.count().label("post_count"),
            func.max(ClusterItem.posted_at).label("last_observed"),
        )
        .where(
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            ClusterItem.cluster_id.isnot(None)
        )
        .group_by(ClusterItem.cluster_id)
        .subquery()
    )


def _problem_statement_discovery():
    return (
        select(
            ClusterItem.problem_statement
        ).where(
            ClusterItem.cluster_id == Cluster.id,
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            _has_text(ClusterItem.problem_statement),
        ).order_by(
            ClusterItem.score.desc().nullslast(),
            ClusterItem.id.asc()
        )
        .limit(1)
        .correlate(Cluster)
        .scalar_subquery()
    )


def to_discovery_card(
        cluster: Cluster,
        *,
        post_count: int,
        last_observed: datetime | None,
        problem_statement: str,
        watched_ids: set[int],
) -> DiscoveryCardResponse: return DiscoveryCardResponse(
    id=cluster.id,
    category=DiscoveryCategory(cluster.discovery_category),
    name=cluster.name,
    summary=cluster.summary,
    problemStatement=problem_statement,
    postCount=post_count,
    date=last_observed,
    isWatched=cluster.id in watched_ids
)


async def load_discovery_row(
        db: AsyncSession,
        *,
        limit: int,
        category: DiscoveryCategory | None = None,
):
    query = build_discovery_query()
    if category is not None:
        query = query.where(
            Cluster.discovery_category == category.value
        )

    query = (
        query.order_by(
            query.selected_columns.last_observed.desc().nullslast(),
            query.selected_columns.post_count.desc(),
            Cluster.id.desc()
        )
        .limit(limit)

    )

    result = await db.execute(query)
    return result.all()


async def load_discovery_dashboard(
        db: AsyncSession,
        uid: str,
) -> DiscoveryDashboardResponse:
    featured_rows = await load_discovery_row(db, limit=1)

    rows_by_category = {}
    for category in DISCOVERY_CATEGORY:
        rows_by_category[category] = await load_discovery_row(
            db,
            limit=DISCOVERY_COLLECTION_LIMIT,
            category=category
        )

    all_rows = list(featured_rows)
    for rows in rows_by_category.values():
        all_rows.extend(rows)

    cluster_ids = sorted({row[0].id for row in all_rows})
    watched_ids = await load_watched_cluster_ids(uid, cluster_ids, db)

    cards_by_id = {
        cluster.id: to_discovery_card(
            cluster,
            post_count=post_count,
            last_observed=last_observed,
            problem_statement=problem_statement,
            watched_ids=watched_ids
        ) for cluster, post_count, last_observed, problem_statement in all_rows
    }

    featured = (cards_by_id[featured_rows[0][0].id]) if featured_rows else None
    collections = [
        DiscoveryCollectionsResponse(
            category=category,
            clusters=[cards_by_id[row[0].id] for row in rows]
        ) for category, rows in rows_by_category.items()
    ]

    return DiscoveryDashboardResponse(
        featured=featured,
        collections=collections
    )
