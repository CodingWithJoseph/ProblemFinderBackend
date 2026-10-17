from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.discovery_contract import DiscoveryCategory, DiscoveryDashboardResponse, DiscoveryCollectionsResponse
from app.models import Cluster
from app.services.cluster_membership import load_watched_cluster_ids
from app.services.cluster_search_text import search_text_expressions
from app.services.discovery import build_discovery_query, DISCOVERY_COLLECTION_LIMIT, to_discovery_card

PREVIEW_SEARCHES = (
    (DiscoveryCategory.BUILD_AND_TOOLING, "build tooling"),
    (DiscoveryCategory.DATA_AND_APIS, "database api"),
    (DiscoveryCategory.WORKFLOW_AND_AUTOMATION, "workflow automation"),
)


async def _load_preview_rows(
        db: AsyncSession,
        *,
        search_text: str,
        limit: int,
):
    term = search_text.strip().lower()
    if not term:
        raise ValueError("Preview search text must not be blank")

    conditions, relevance = search_text_expressions(term)

    query = build_discovery_query()
    query = (
        query
        .where(or_(*conditions))
        .order_by(
            relevance.desc(),
            query.selected_columns.last_observed.desc().nullslast(),
            query.selected_columns.post_count.desc(),
            Cluster.id.desc()
        )
        .limit(limit)
    )

    result = await db.execute(query)
    return result.all()


async def load_discovery_preview_dashboard(db: AsyncSession, uid: str) -> DiscoveryDashboardResponse:
    rows_by_category = {}
    seen_ids: set[int] = set()

    for category, search_text in PREVIEW_SEARCHES:
        matches = await _load_preview_rows(
            db,
            search_text=search_text,
            limit=DISCOVERY_COLLECTION_LIMIT
        )

        rows = [row for row in matches if row[0].id not in seen_ids]
        seen_ids.update(row[0].id for row in rows)
        rows_by_category[category] = rows

    watched_ids = await load_watched_cluster_ids(
        uid,
        sorted(seen_ids),
        db
    )

    collections = []
    for category, rows in rows_by_category.items():
        cards = [
            to_discovery_card(
                cluster=cluster,
                post_count=post_count,
                last_observed=last_observed,
                problem_statement=problem_statement,
                watched_ids=watched_ids
            ).model_copy(update={"category": category}) for cluster, post_count, last_observed, problem_statement in
            rows
        ]

        collections.append(
            DiscoveryCollectionsResponse(
                category=category,
                clusters=cards
            )
        )

    featured = next(
        (
            collection.clusters[0]
            for collection in collections
            if collection.clusters
        ),
        None
    )

    return DiscoveryDashboardResponse(
        featured=featured,
        collections=collections
    )
