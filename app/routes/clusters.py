
"""Cluster discovery and detail over the v2 public AI publish contract.

Every post-level read uses ``cluster_items`` filtered to ``PIPELINE_VERSION``.
Searchable enrichment is limited to fields the current publisher still writes:
source text, community metadata, engagement, and ``problem_statement``.
"""
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.auth import get_uid
from app.config import PIPELINE_VERSION
from app.models import Cluster, ClusterItem, Pipeline
from app.search_agent_contract import SearchAgentResponse
from app.search_contract import (
    ClusterSearchQuery,
    SearchFilterOptions,
    SearchInterpretationResponse,
    SearchInterpretRequest,
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
)
from app.search_normalization import meaningful_search_terms
from app.services.search_agent import run_search_agent
from app.services.search_interpreter import interpret_search, load_search_filter_options

router = APIRouter(prefix="/clusters", tags=["clusters"])

TOP_EVIDENCE_COUNT = 4

SEARCH_SAMPLE_POSTS = 3
SEARCH_MAX_TOKENS = 12


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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# cluster_items helpers (shared with pipeline.py / problems.py / dashboard.py)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
        .order_by(ClusterItem.score.desc().nullslast())
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Watched-cluster state (user pipeline cards)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def _watched_cluster_ids(uid: str, cluster_ids: list[int], db: AsyncSession) -> set[int]:
    if not cluster_ids:
        return set()
    str_ids = [str(cid) for cid in cluster_ids]
    rows = (await db.execute(
        select(Pipeline.source_cluster_id).where(
            Pipeline.user_id == uid,
            Pipeline.source_cluster_id.in_(str_ids),
            Pipeline.removed_at == None,
        )
    )).all()
    watched: set[int] = set()
    for (scid,) in rows:
        try:
            watched.add(int(scid))
        except (TypeError, ValueError):
            continue
    return watched


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Serializers
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


def _serialize_discovery_cluster(c: Cluster, items: list[ClusterItem], watched_ids: set[int]) -> dict:
    subreddits = sorted({it.community for it in items if it.community})
    dates = sorted(_iso(_item_observed_at(it)) for it in items if _item_observed_at(it))
    return {
        "id": c.id,
        "name": c.name,
        "summary": c.summary,
        "problemStatement": representative_problem_statement(items),
        "post_count": len(items),
        "date_range_start": dates[0] if dates else None,
        "date_range_end": dates[-1] if dates else None,
        "sources": _source_names(items),
        "subreddits": subreddits,
        "sample_posts": [serialize_sample_item(it) for it in items[:SEARCH_SAMPLE_POSTS]],
        "is_watched": c.id in watched_ids,
    }


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Discovery: search + browse
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


def _contains_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _item_text_match(pattern: str):
    exists_q = select(ClusterItem.id).where(
        ClusterItem.cluster_id == Cluster.id,
        ClusterItem.pipeline_version == PIPELINE_VERSION,
        or_(
            ClusterItem.title.ilike(pattern, escape="\\"),
            ClusterItem.body.ilike(pattern, escape="\\"),
            ClusterItem.problem_statement.ilike(pattern, escape="\\"),
            ClusterItem.platform.ilike(pattern, escape="\\"),
            ClusterItem.community.ilike(pattern, escape="\\"),
        ),
    )
    return exists_q.exists()


def _search_text_expressions(query: str):
    conditions = []
    score_parts = []
    meaningful_terms = meaningful_search_terms(query)
    normalized_phrase = " ".join(meaningful_terms) or query
    terms: list[tuple[str, tuple[int, int, int]]] = [(normalized_phrase, (8, 5, 3))]
    seen = {normalized_phrase.casefold()}
    for token in meaningful_terms:
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append((token, (3, 2, 1)))
        if len(terms) >= SEARCH_MAX_TOKENS + 1:
            break

    for term, weights in terms:
        pattern = _contains_pattern(term)
        name_match = Cluster.name.ilike(pattern, escape="\\")
        summary_match = Cluster.summary.ilike(pattern, escape="\\")
        item_match = _item_text_match(pattern)
        conditions.extend([name_match, summary_match, item_match])
        score_parts.extend([
            case((name_match, weights[0]), else_=0),
            case((summary_match, weights[1]), else_=0),
            case((item_match, weights[2]), else_=0),
        ])

    relevance = sum(score_parts, literal(0))
    return conditions, relevance


def _build_cluster_search(filters: ClusterSearchQuery):
    """Compile allow-listed search filters into a parameterized SQLAlchemy query."""
    stats = _item_stats_subquery()
    base = (
        select(Cluster)
        .join(stats, stats.c.cluster_id == Cluster.id)
        .where(
            Cluster.pipeline_version == PIPELINE_VERSION,
            stats.c.post_count >= filters.min_posts,
        )
    )

    relevance = None
    if filters.query:
        conditions, relevance = _search_text_expressions(filters.query)
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


async def _materialize_discovery(base, order_clauses, limit, offset, uid, db) -> dict:
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    query = base.order_by(*order_clauses).offset(offset).limit(limit)
    clusters = list((await db.execute(query)).scalars().all())

    cluster_ids = [c.id for c in clusters]
    items_map = await _items_by_cluster(cluster_ids, db)
    watched_ids = await _watched_cluster_ids(uid, cluster_ids, db)

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
    """Execute a confirmed, allow-listed search draft against cluster data."""
    base, order_clauses = _build_cluster_search(body)
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Cluster detail + sub-resources
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def _fetch_cluster(cluster_id: int, db: AsyncSession) -> Cluster | None:
    result = await db.execute(
        select(Cluster).where(
            Cluster.id == cluster_id,
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
    )
    return result.scalar_one_or_none()


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

    return {
        **_serialize_cluster(cluster, all_items, post_count=total_posts),
        "problemStatement": representative_problem_statement(top_items),
        "topEvidence": [serialize_evidence_item(it) for it in top_items],
        "signals": {"totalPosts": total_posts},
        "postIds": all_ids,
        "posts": [serialize_evidence_item(it) for it in page_items],
        "page": page,
        "pageSize": pageSize,
        "totalPosts": total_posts,
    }
