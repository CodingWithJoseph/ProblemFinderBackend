"""Cluster discovery and detail over the v2 public AI publish contract.

Every post-level read uses ``cluster_items`` filtered to ``PIPELINE_VERSION``.
Searchable enrichment is limited to fields the current publisher still writes:
source text, community metadata, engagement, and ``problem_statement``.
"""
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.browse_contract import (
    BrowseCategory,
    BrowsePageResponse,
    BrowseSort,
    DiscoveryClusterResponse,
    build_browse_filters,
)
from app.browse_cursor import (
    BrowseCursor,
    decode_browse_cursor,
    encode_browse_cursor,
)
from app.services.cluster_text import has_nonblank_text
from app.config import PIPELINE_VERSION
from app.db import get_db
from app.discovery_contract import DiscoveryDashboardResponse
from app.models import Cluster, ClusterItem
from app.search_agent_contract import SearchAgentResponse
from app.search_contract import (
    ClusterSearchQuery,
    SearchFilterOptions,
    SearchInterpretationResponse,
    SearchInterpretRequest,
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
)
from app.services.cluster_membership import load_watched_cluster_ids
from app.services.cluster_search_text import search_text_expressions
from app.services.search_agent import run_search_agent
from app.services.search_interpreter import interpret_search, load_search_filter_options
from app.services.temp_discovery_preview import load_discovery_preview_dashboard

router = APIRouter(prefix="/clusters", tags=["clusters"])

TOP_EVIDENCE_COUNT = 4
SEARCH_SAMPLE_POSTS = 3


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _source_names(items: list[ClusterItem]) -> list[str]:
    return sorted({
        source.strip()
        for source in (item.platform for item in items)
        if source and source.strip()
    })


def _item_observed_at(it: ClusterItem):
    return it.posted_at


def serialize_sample_item(it: ClusterItem) -> dict:
    return {
        "id": it.id,
        "title": it.title,
        "subreddit": it.community,
        "reddit_score": it.score,
    }


def serialize_evidence_item(it: ClusterItem) -> dict:
    """Full side-by-side post view used by the Evidence and detail views.

    ``upvoteRatio`` / ``topComments`` have no first-class column in the
    simplified schema; they are best-effort recovered from ``raw_json``.
    """
    raw = it.raw_json if isinstance(it.raw_json, dict) else {}
    return {
        "id": it.id,
        "title": it.title or "Untitled post",
        "body": it.body,
        "excerpt": (it.body or "")[:300],
        "url": it.url,
        "author": it.author,
        "community": it.community or it.platform,
        "source": it.platform,
        "score": it.score,
        "engagement": it.score,
        "numComments": it.num_comments,
        "upvoteRatio": raw.get("upvote_ratio"),
        "topComments": raw.get("top_comments") or [],
        "problemStatement": it.problem_statement,
        "similarityScore": it.similarity_score,
        "postedAt": _iso(it.posted_at),
        "observedAt": _iso(_item_observed_at(it)),
        "dateType": "posted" if it.posted_at else "observed",
    }


def representative_problem_statement(items: list[ClusterItem]) -> str | None:
    """The highest-scored item's problem statement (items are score-sorted)."""
    for it in items:
        if it.problem_statement and it.problem_statement.strip():
            return it.problem_statement.strip()
    return None


def _sort_items(items: list[ClusterItem]) -> list[ClusterItem]:
    items.sort(key=lambda it: (it.score is not None, it.score or 0), reverse=True)
    return items


async def load_cluster_items(
        db: AsyncSession,
        cluster_id: int,
        *,
        limit: int | None = None,
        offset: int | None = None,
) -> list[ClusterItem]:
    """Items for a cluster, highest reddit score first."""
    query = (
        select(ClusterItem)
        .where(
            ClusterItem.cluster_id == cluster_id,
            ClusterItem.pipeline_version == PIPELINE_VERSION,
        )
        .order_by(ClusterItem.score.desc().nullslast(), ClusterItem.id.asc())
    )
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return list((await db.execute(query)).scalars().all())


async def count_cluster_items(db: AsyncSession, cluster_id: int) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(ClusterItem)
        .where(
            ClusterItem.cluster_id == cluster_id,
            ClusterItem.pipeline_version == PIPELINE_VERSION,
        )
    ) or 0


async def _items_by_cluster(cluster_ids: list[int], db: AsyncSession) -> dict[int, list[ClusterItem]]:
    if not cluster_ids:
        return {}
    rows = (await db.execute(
        select(ClusterItem).where(
            ClusterItem.cluster_id.in_(cluster_ids),
            ClusterItem.pipeline_version == PIPELINE_VERSION,
        )
    )).scalars().all()
    grouped: dict[int, list[ClusterItem]] = {}
    for it in rows:
        grouped.setdefault(it.cluster_id, []).append(it)
    for items in grouped.values():
        _sort_items(items)
    return grouped


def _item_stats_subquery():
    """Per-cluster post_count + most-recent observed date over cluster_items."""
    return (
        select(
            ClusterItem.cluster_id.label("cluster_id"),
            func.count().label("post_count"),
            func.max(ClusterItem.posted_at).label("last_observed"),
        )
        .where(
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            ClusterItem.cluster_id.isnot(None),
        )
        .group_by(ClusterItem.cluster_id)
        .subquery()
    )


def _serialize_cluster(c: Cluster, items: list[ClusterItem], *, post_count: int | None = None) -> dict:
    observed = sorted(
        timestamp
        for timestamp in (_item_observed_at(item) for item in items)
        if timestamp is not None
    )
    sources = Counter(item.platform for item in items if item.platform)
    volume = Counter(timestamp.date().isoformat() for timestamp in observed)
    return {
        "id": c.id,
        "name": c.name,
        "summary": c.summary,
        "firstSeen": _iso(observed[0]) if observed else None,
        "lastSeenDate": _iso(observed[-1]) if observed else None,
        "authorCount": len({item.author for item in items if item.author}),
        "communityCount": len({item.community for item in items if item.community}),
        "sourceBreakdown": [
            {"platform": platform, "count": count}
            for platform, count in sorted(sources.items())
        ],
        "postVolumeByDate": [
            {"date": date, "count": count}
            for date, count in sorted(volume.items())
        ],
        "postCount": post_count,
    }


def _serialize_browse_clusters(
        cluster: Cluster,
        *,
        post_count: int,
        last_observed: datetime | None,
        problem_statement: str,
        watched_ids: set[int],
) -> DiscoveryClusterResponse:
    return DiscoveryClusterResponse(
        id=cluster.id,
        category=cluster.discovery_category,
        name=cluster.name,
        summary=cluster.summary,
        problemStatement=problem_statement,
        postCount=post_count,
        isWatched=cluster.id in watched_ids,
        date=last_observed
    )


def _serialize_discovery_cluster(c: Cluster, items: list[ClusterItem], watched_ids: set[int]) -> dict:
    subreddits = sorted({it.community for it in items if it.community})
    dates = sorted(_iso(_item_observed_at(it)) for it in items if _item_observed_at(it))
    return {
        "id": c.id,
        "name": c.name,
        "summary": c.summary,
        "category": c.discovery_category,
        "problemStatement": representative_problem_statement(items),
        "post_count": len(items),
        "date_range_start": dates[0] if dates else None,
        "date_range_end": dates[-1] if dates else None,
        "sources": _source_names(items),
        "subreddits": subreddits,
        "sample_posts": [serialize_sample_item(it) for it in items[:SEARCH_SAMPLE_POSTS]],
        "is_watched": c.id in watched_ids,
    }


def _skip_filter(value: str | None) -> bool:
    return value is None or value.strip() == "" or value.strip().lower() == "all"


def _normalized_values(values: list[str]) -> list[str]:
    return [value.strip().lower() for value in values if not _skip_filter(value)]


def _apply_item_value_filter(base, column, values: list[str]):
    normalized = _normalized_values(values)
    if not normalized:
        return base
    exists_q = select(ClusterItem.id).where(
        ClusterItem.cluster_id == Cluster.id,
        ClusterItem.pipeline_version == PIPELINE_VERSION,
        func.lower(func.trim(column)).in_(normalized),
    )
    return base.where(exists_q.exists())


def _build_cluster_search(filters: ClusterSearchQuery):
    """Compile allow-listed search filters into a parameterized SQLAlchemy query."""
    stats = _item_stats_subquery()
    base = (
        select(Cluster, stats.c.post_count, stats.c.last_observed)
        .join(stats, stats.c.cluster_id == Cluster.id)
        .where(
            Cluster.pipeline_version == PIPELINE_VERSION,
            stats.c.post_count >= filters.min_posts,
        )
    )

    relevance = None
    if filters.query:
        conditions, relevance = search_text_expressions(filters.query)
        base = base.where(or_(*conditions))

    base = _apply_item_value_filter(base, ClusterItem.platform, filters.sources)
    base = _apply_item_value_filter(base, ClusterItem.community, filters.communities)

    if filters.observed_after:
        base = base.where(stats.c.last_observed >= filters.observed_after)

    freshness_order = [
        stats.c.last_observed.desc().nullslast(),
    ]
    newest_order = [
        *freshness_order,
        stats.c.post_count.desc(),
    ]
    if filters.sort == "largest":
        order_clauses = [stats.c.post_count.desc(), *freshness_order]
    elif filters.sort == "relevance" and relevance is not None:
        order_clauses = [relevance.desc(), stats.c.post_count.desc(), *freshness_order]
    else:
        order_clauses = newest_order

    return base, order_clauses


def _browse_sort_fields(base, sort: BrowseSort, cursor: BrowseCursor | None = None):
    count = base.selected_columns.post_count
    observed = base.selected_columns.last_observed
    count_value = cursor.post_count if cursor else None
    date_value = cursor.last_observed if cursor else None
    id_value = cursor.cluster_id if cursor else None
    date_field = (observed, date_value, sort == BrowseSort.OLDEST, True)
    count_field = (count, count_value, sort == BrowseSort.SMALLEST, False)
    fields = [count_field, date_field] if sort in (BrowseSort.LARGEST, BrowseSort.SMALLEST) else [date_field, count_field]
    return [*fields, (Cluster.id, id_value, False, False)]


def _browse_order(base, sort: BrowseSort):
    return [
        (column.asc() if ascending else column.desc()).nullslast() if nullable
        else (column.asc() if ascending else column.desc())
        for column, _, ascending, nullable in _browse_sort_fields(base, sort)
    ]


def _apply_browse_cursor(base, cursor: BrowseCursor):
    # Lexicographic keyset pagination must use the exact same keys as ORDER BY.
    # Missing activity dates sort last in both chronological directions.
    prefix = []
    alternatives = []
    for column, value, ascending, nullable in _browse_sort_fields(base, cursor.sort, cursor):
        if value is None:
            prefix.append(column.is_(None))
            continue
        after = column > value if ascending else column < value
        if nullable:
            after = or_(after, column.is_(None))
        alternatives.append(and_(*prefix, after))
        prefix.append(column == value)
    return base.where(or_(*alternatives))


async def _load_browse_rows(
        filters: ClusterSearchQuery,
        limit: int,
        db: AsyncSession,
        *,
        category: BrowseCategory = BrowseCategory.ALL,
        cursor: BrowseCursor | None = None,
):
    base, _ = _build_cluster_search(filters)
    order_clauses = _browse_order(base, BrowseSort(filters.sort))

    if category != BrowseCategory.ALL:
        base = base.where(Cluster.discovery_category == category)

    if cursor is not None:
        base = _apply_browse_cursor(base, cursor)

    statement_query = (
        select(
            ClusterItem.problem_statement
        ).where(
            ClusterItem.cluster_id == Cluster.id,
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            has_nonblank_text(ClusterItem.problem_statement),
        ).order_by(
            ClusterItem.score.desc().nullslast(),
            ClusterItem.id.asc()
        ).limit(1)
        .correlate(Cluster)
        .scalar_subquery()
    )

    query = (
        base.add_columns(statement_query.label("problem_statement"))
        .where(
            has_nonblank_text(Cluster.name),
            has_nonblank_text(Cluster.summary),
            statement_query.isnot(None),
        )
        .order_by(*order_clauses)
        .limit(limit + 1)
    )

    result = await db.execute(query)
    rows = result.all()
    return rows[:limit], len(rows) > limit


async def _load_browse_clusters(
        filters: ClusterSearchQuery,
        limit: int,
        uid: str,
        db: AsyncSession,
        *,
        category: BrowseCategory = BrowseCategory.ALL,
        cursor: BrowseCursor | None = None,
) -> tuple[list[DiscoveryClusterResponse], bool]:
    rows, has_more = await _load_browse_rows(
        filters=filters,
        limit=limit,
        db=db,
        category=category,
        cursor=cursor,
    )

    cluster_ids = [row[0].id for row in rows]
    watched_ids = await load_watched_cluster_ids(uid, cluster_ids, db)

    clusters = [
        _serialize_browse_clusters(
            cluster,
            post_count=post_count,
            last_observed=last_observed,
            problem_statement=problem_statement,
            watched_ids=watched_ids,
        )
        for cluster, post_count, last_observed, problem_statement in rows
    ]

    return clusters, has_more


async def _materialize_discovery(base, order_clauses, limit, offset, uid, db) -> dict:
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    query = base.order_by(*order_clauses).offset(offset).limit(limit)
    clusters = list((await db.execute(query)).scalars().all())

    cluster_ids = [c.id for c in clusters]
    items_map = await _items_by_cluster(cluster_ids, db)
    watched_ids = await load_watched_cluster_ids(uid, cluster_ids, db)

    return {
        "data": [
            _serialize_discovery_cluster(c, items_map.get(c.id, []), watched_ids)
            for c in clusters
        ],
        "total": total or 0,
    }


@router.get("/search")
async def search_clusters(
        q: str = Query(..., min_length=2, max_length=500),
        limit: int = Query(SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT),
        offset: int = Query(0, ge=0, le=1_000_000),
        min_posts: int = Query(1, ge=1, le=100_000),
        db: AsyncSession = Depends(get_db),
        uid: str = Depends(get_uid),
):
    """Cluster-first search across cluster and item text."""
    filters = ClusterSearchQuery(
        query=q,
        min_posts=min_posts,
        sort="largest",
        limit=limit,
        offset=offset,
    )
    base, order_clauses = _build_cluster_search(filters)
    return await _materialize_discovery(base, order_clauses, limit, offset, uid, db)


@router.post("/search/query")
async def query_clusters(
        body: ClusterSearchQuery,
        db: AsyncSession = Depends(get_db),
        uid: str = Depends(get_uid),
):
    """Execute a confirmed, allow-listed search draft against displayable clusters."""
    base, order_clauses = _build_cluster_search(body)
    has_statement = select(ClusterItem.id).where(
        ClusterItem.cluster_id == Cluster.id,
        ClusterItem.pipeline_version == PIPELINE_VERSION,
        has_nonblank_text(ClusterItem.problem_statement),
    ).exists()
    base = base.where(has_nonblank_text(Cluster.name), has_nonblank_text(Cluster.summary), has_statement)
    result = await _materialize_discovery(
        base,
        order_clauses,
        body.limit,
        body.offset,
        uid,
        db,
    )
    returned = len(result["data"])
    has_more = body.offset + returned < result["total"]
    return {
        **result,
        "applied_filters": body.applied_filters(),
        "pagination": {
            "limit": body.limit,
            "offset": body.offset,
            "returned": returned,
            "has_more": has_more,
            "next_offset": body.offset + body.limit if has_more else None,
        },
    }


@router.get("/search/options", response_model=SearchFilterOptions)
async def search_filter_options(
        db: AsyncSession = Depends(get_db),
        uid: str = Depends(get_uid),
):
    """Canonical values available to structured filters and the filter editor."""
    try:
        return await load_search_filter_options(db)
    except SQLAlchemyError:
        await db.rollback()
        raise HTTPException(status_code=503, detail="Search filter options are temporarily unavailable")


@router.post("/search/interpret", response_model=SearchInterpretationResponse)
async def interpret_cluster_search(
        body: SearchInterpretRequest,
        db: AsyncSession = Depends(get_db),
        uid: str = Depends(get_uid),
):
    """Translate one conversational turn into a draft without running search."""
    return await interpret_search(body, uid, db)


@router.post("/search/agent", response_model=SearchAgentResponse)
async def run_cluster_search_agent(
        body: SearchInterpretRequest,
        db: AsyncSession = Depends(get_db),
        uid: str = Depends(get_uid),
):
    """Run a bounded tool loop that always stops before database search execution."""
    return await run_search_agent(body, uid, db)


@router.get("/browse")
async def browse_clusters(
        limit: int = Query(SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT),
        offset: int = Query(0, ge=0, le=1_000_000),
        sort: str = Query("newest"),
        subreddit: str | None = Query(None, max_length=100),
        min_posts: int = Query(1, ge=1, le=100_000),
        db: AsyncSession = Depends(get_db),
        uid: str = Depends(get_uid),
):
    """Default (no-query) cluster discovery feed with sort + subreddit filter."""
    stats = _item_stats_subquery()
    base = (
        select(Cluster)
        .join(stats, stats.c.cluster_id == Cluster.id)
        .where(
            Cluster.pipeline_version == PIPELINE_VERSION,
            stats.c.post_count >= min_posts,
        )
    )
    if subreddit:
        item_exists = select(ClusterItem.id).where(
            ClusterItem.cluster_id == Cluster.id,
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            ClusterItem.community == subreddit,
        )
        base = base.where(item_exists.exists())

    newest_order = [
        stats.c.last_observed.desc().nullslast(),
    ]
    if sort == "largest":
        order_clauses = [stats.c.post_count.desc(), *newest_order]
    else:  # "newest" (default)
        order_clauses = [*newest_order, stats.c.post_count.desc()]

    return await _materialize_discovery(base, order_clauses, limit, offset, uid, db)


@router.get("/browse/page", response_model=BrowsePageResponse)
async def browse_cluster_page(
        category: BrowseCategory = Query(BrowseCategory.ALL),
        sort: BrowseSort = Query(BrowseSort.NEWEST),
        limit: int = Query(
            SEARCH_DEFAULT_LIMIT,
            ge=1,
            le=SEARCH_MAX_LIMIT,
        ),
        cursor: str | None = Query(
            None,
            min_length=1,
            max_length=2048,
        ),
        db: AsyncSession = Depends(get_db),
        uid: str = Depends(get_uid),
) -> BrowsePageResponse:
    decoded_cursor = None
    if cursor is not None:
        try:
            decoded_cursor = decode_browse_cursor(
                cursor,
                category=category,
                sort=sort,
                pipeline_version=PIPELINE_VERSION,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=str(exc),
            ) from exc

    filters = build_browse_filters(sort)

    clusters, has_more = await _load_browse_clusters(
        filters,
        limit,
        uid,
        db,
        category=category,
        cursor=decoded_cursor,
    )

    next_cursor = None
    if has_more:
        last_cluster = clusters[-1]
        next_cursor = encode_browse_cursor(
            BrowseCursor(
                version=1,
                pipeline_version=PIPELINE_VERSION,
                category=category,
                sort=sort,
                cluster_id=last_cluster.id,
                post_count=last_cluster.postCount,
                last_observed=last_cluster.date,
            )
        )

    return BrowsePageResponse(
        clusters=clusters,
        nextCursor=next_cursor,
    )


async def _fetch_cluster(cluster_id: int, db: AsyncSession) -> Cluster | None:
    result = await db.execute(
        select(Cluster).where(
            Cluster.id == cluster_id,
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
    )
    return result.scalar_one_or_none()


@router.get("/discovery", response_model=DiscoveryDashboardResponse)
async def get_discovery_dashboard(
        db: AsyncSession = Depends(get_db),
        uid: str = Depends(get_uid),
) -> DiscoveryDashboardResponse:
    return await load_discovery_preview_dashboard(db, uid)


@router.get("/{cluster_id}")
async def get_cluster(
        cluster_id: int,
        page: int = Query(1, ge=1),
        pageSize: int = Query(20, ge=1, le=200),
        db: AsyncSession = Depends(get_db),
        uid: str = Depends(get_uid),
):
    """Problem breakdown with source evidence and paginated cluster items."""
    cluster = await _fetch_cluster(cluster_id, db)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    total_posts = await count_cluster_items(db, cluster_id)
    top_items = await load_cluster_items(db, cluster_id, limit=TOP_EVIDENCE_COUNT)
    page_items = await load_cluster_items(db, cluster_id, limit=pageSize, offset=(page - 1) * pageSize)
    all_items = await load_cluster_items(db, cluster_id)
    all_ids = [item.id for item in all_items]
    watched_ids = await load_watched_cluster_ids(uid, [cluster_id], db)

    return {
        **_serialize_cluster(cluster, all_items, post_count=total_posts),
        "problemStatement": representative_problem_statement(all_items),
        "category": cluster.discovery_category,
        "isWatched": cluster_id in watched_ids,
        "topEvidence": [serialize_evidence_item(it) for it in top_items],
        "signals": {"totalPosts": total_posts},
        "postIds": all_ids,
        "posts": [serialize_evidence_item(it) for it in page_items],
        "page": page,
        "pageSize": pageSize,
        "totalPosts": total_posts,
    }
