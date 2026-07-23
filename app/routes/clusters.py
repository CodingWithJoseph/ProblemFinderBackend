"""Cluster discovery + detail, backed entirely by the flat ``cluster_items`` table.

The simplified 6-table schema collapsed ``source_posts`` ⋈ ``cluster_members`` ⋈
``opportunities`` into one ``cluster_items`` row per post. Every post-level read
in this module is a single-table ``cluster_items`` query filtered to the current
``PIPELINE_VERSION``. Clusters are ranked by the pipeline's ``signal_score``.

The per-item ``problem_statement`` / ``solution_angle`` (LLM-derived) power the
MVP Problem-breakdown (cluster detail), Evidence (items) and Task-list
(distinct solution angles) views.
"""
from collections import Counter
from datetime import datetime, timezone
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.auth import get_uid
from app.config import PIPELINE_VERSION, Settings, get_settings
from app.models import Cluster, ClusterItem, ClusterSnapshot, ClusterSignal, Pipeline
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
from app.services.web_search import WebSearchFailure, search_web_evidence
from app.web_search_contract import WebSearchRequest, WebSearchResponse

router = APIRouter(prefix="/clusters", tags=["clusters"])
logger = logging.getLogger(__name__)

SUFFICIENT_HISTORY_DAYS = 7
TASKS_SCAN_LIMIT = 500
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


def _most_common(values) -> str | None:
    cleaned = [v for v in values if v]
    if not cleaned:
        return None
    return Counter(cleaned).most_common(1)[0][0]


def _source_names(c: Cluster, items: list[ClusterItem]) -> list[str]:
    sources: list[str] = []
    cluster_source = getattr(c, "source", None)
    if cluster_source:
        sources.append(cluster_source)
    for row in c.source_breakdown or []:
        if isinstance(row, dict):
            source = row.get("source") or row.get("platform")
            if source:
                sources.append(str(source))
    sources.extend(it.platform for it in items if it.platform)
    return sorted({source.strip() for source in sources if source and source.strip()})


def _signal_score(c: Cluster, signal: ClusterSignal | None = None) -> float | None:
    if signal and signal.signal_score is not None:
        return signal.signal_score
    return c.signal_score


def _signal_score_order():
    return func.coalesce(ClusterSignal.signal_score, Cluster.signal_score)


async def _signals_by_cluster(cluster_ids: list[int], db: AsyncSession) -> dict[int, ClusterSignal]:
    if not cluster_ids:
        return {}
    try:
        rows = (await db.execute(
            select(ClusterSignal).where(ClusterSignal.cluster_id.in_(cluster_ids))
        )).scalars().all()
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.warning("cluster_signals unavailable; falling back to clusters.signal_score", exc_info=True)
        return {}
    return {row.cluster_id: row for row in rows}


async def _signal_for_cluster(cluster_id: int, db: AsyncSession) -> ClusterSignal | None:
    try:
        return (await db.execute(
            select(ClusterSignal).where(ClusterSignal.cluster_id == cluster_id)
        )).scalar_one_or_none()
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.warning("cluster_signals unavailable for cluster %s", cluster_id, exc_info=True)
        return None


# ──────────────────────────────────────────────────────────────────────────
# cluster_items helpers (shared with pipeline.py / problems.py / dashboard.py)
# ──────────────────────────────────────────────────────────────────────────

def _item_observed_at(it: ClusterItem):
    return it.posted_at or it.scraped_at


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
        "url": it.url or it.permalink,
        "author": it.author,
        "community": it.community or it.platform,
        "source": it.platform,
        "score": it.score,
        "engagement": it.score,
        "numComments": it.num_comments,
        "upvoteRatio": raw.get("upvote_ratio"),
        "topComments": raw.get("top_comments") or [],
        "opportunityType": it.opportunity_type,
        "opportunityDomain": it.opportunity_domain,
        "problemStatement": it.problem_statement,
        "solutionAngle": it.solution_angle,
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


def distinct_solution_angles(items: list[ClusterItem]) -> list[dict]:
    """De-duplicated solution angles for a cluster, ranked by best supporting
    post (``items`` must already be score-sorted desc)."""
    seen: dict[str, dict] = {}
    for it in items:
        angle = (it.solution_angle or "").strip()
        if not angle:
            continue
        key = angle.lower()
        if key not in seen:
            seen[key] = {
                "solutionAngle": angle,
                "problemStatement": (it.problem_statement or "").strip() or None,
                "evidenceCount": 1,
                "topPostId": it.id,
                "topScore": it.score,
            }
        else:
            seen[key]["evidenceCount"] += 1
    return list(seen.values())


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
            func.max(func.coalesce(ClusterItem.posted_at, ClusterItem.scraped_at)).label("last_observed"),
        )
        .where(
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            ClusterItem.cluster_id.isnot(None),
        )
        .group_by(ClusterItem.cluster_id)
        .subquery()
    )


# ──────────────────────────────────────────────────────────────────────────
# Watched-cluster state (user pipeline cards)
# ──────────────────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────────────────
# Serializers
# ──────────────────────────────────────────────────────────────────────────

def _serialize_cluster(c: Cluster, *, post_count: int | None = None, signal: ClusterSignal | None = None) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "summary": c.summary,
        "signalScore": _signal_score(c, signal),
        "trending": c.trending,
        "firstSeen": c.first_seen.isoformat() if c.first_seen else None,
        "lastSeenDate": c.last_seen_date.isoformat() if c.last_seen_date else None,
        "authorCount": c.author_count,
        "communityCount": c.community_count,
        "sourceBreakdown": c.source_breakdown or [],
        "postVolumeByDate": c.post_volume_by_date or [],
        "postCount": post_count,
    }


def _serialize_discovery_cluster(c: Cluster, items: list[ClusterItem], watched_ids: set[int], signal: ClusterSignal | None = None) -> dict:
    subreddits = sorted({it.community for it in items if it.community})
    dates = sorted(_iso(_item_observed_at(it)) for it in items if _item_observed_at(it))
    return {
        "id": c.id,
        "name": c.name,
        "summary": c.summary,
        "signalScore": _signal_score(c, signal),
        "opportunity_type": _most_common(it.opportunity_type for it in items),
        "opportunity_domain": _most_common(it.opportunity_domain for it in items),
        "problemStatement": representative_problem_statement(items),
        "post_count": len(items),
        "trending_status": "trending" if c.trending else None,
        "date_range_start": dates[0] if dates else None,
        "date_range_end": dates[-1] if dates else None,
        "sources": _source_names(c, items),
        "subreddits": subreddits,
        "sample_posts": [serialize_sample_item(it) for it in items[:SEARCH_SAMPLE_POSTS]],
        "is_watched": c.id in watched_ids,
    }


# ──────────────────────────────────────────────────────────────────────────
# Discovery: search + browse
# ──────────────────────────────────────────────────────────────────────────

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
            ClusterItem.problem_statement.ilike(pattern, escape="\\"),
            ClusterItem.solution_angle.ilike(pattern, escape="\\"),
            ClusterItem.opportunity_type.ilike(pattern, escape="\\"),
            ClusterItem.opportunity_domain.ilike(pattern, escape="\\"),
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

    base = _apply_item_value_filter(base, ClusterItem.opportunity_domain, filters.opportunity_domains)
    base = _apply_item_value_filter(base, ClusterItem.opportunity_type, filters.opportunity_types)
    base = _apply_item_value_filter(base, ClusterItem.platform, filters.sources)
    base = _apply_item_value_filter(base, ClusterItem.community, filters.communities)

    if filters.observed_after:
        base = base.where(stats.c.last_observed >= filters.observed_after)
    if filters.trending_only:
        base = base.where(Cluster.trending.is_(True))

    needs_signal_join = filters.min_signal_score is not None or filters.sort == "signal_score"
    if needs_signal_join:
        base = base.outerjoin(ClusterSignal, ClusterSignal.cluster_id == Cluster.id)
    if filters.min_signal_score is not None:
        base = base.where(_signal_score_order() >= filters.min_signal_score)

    freshness_order = [
        stats.c.last_observed.desc().nullslast(),
        Cluster.last_seen_date.desc().nullslast(),
    ]
    newest_order = [
        *freshness_order,
        stats.c.post_count.desc(),
    ]
    if filters.sort == "largest":
        order_clauses = [stats.c.post_count.desc(), *freshness_order]
    elif filters.sort == "trending":
        order_clauses = [Cluster.trending.desc(), *newest_order]
    elif filters.sort == "signal_score":
        order_clauses = [_signal_score_order().desc().nullslast(), *newest_order]
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
    signal_map = await _signals_by_cluster(cluster_ids, db)

    return {
        "data": [
            _serialize_discovery_cluster(c, items_map.get(c.id, []), watched_ids, signal_map.get(c.id))
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
    opportunity_domain: str | None = Query(None, max_length=100),
    opportunity_type: str | None = Query(None, max_length=100),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Cluster-first search across cluster and item text."""
    filters = ClusterSearchQuery(
        query=q,
        opportunity_domains=[] if _skip_filter(opportunity_domain) else [opportunity_domain],
        opportunity_types=[] if _skip_filter(opportunity_type) else [opportunity_type],
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


@router.post("/search/web", response_model=WebSearchResponse)
async def search_external_evidence(
    body: WebSearchRequest,
    uid: str = Depends(get_uid),
    settings: Settings = Depends(get_settings),
):
    """Run a gated external search only after explicit user confirmation."""
    _ = uid
    if not settings.search_web_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        return await search_web_evidence(body, settings)
    except WebSearchFailure as exc:
        if exc.category == "timeout":
            raise HTTPException(status_code=504, detail="External search timed out") from None
        raise HTTPException(status_code=503, detail="External search is unavailable") from None


@router.get("/browse")
async def browse_clusters(
    limit: int = Query(SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT),
    offset: int = Query(0, ge=0, le=1_000_000),
    sort: str = Query("newest"),
    subreddit: str | None = Query(None, max_length=100),
    min_posts: int = Query(1, ge=1, le=100_000),
    opportunity_domain: str | None = Query(None, max_length=100),
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
    base = _apply_item_value_filter(
        base,
        ClusterItem.opportunity_domain,
        [] if _skip_filter(opportunity_domain) else [opportunity_domain],
    )

    if subreddit:
        item_exists = select(ClusterItem.id).where(
            ClusterItem.cluster_id == Cluster.id,
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            ClusterItem.community == subreddit,
        )
        base = base.where(item_exists.exists())

    newest_order = [
        Cluster.last_seen_date.desc().nullslast(),
        stats.c.last_observed.desc().nullslast(),
    ]
    if sort == "largest":
        order_clauses = [stats.c.post_count.desc(), *newest_order]
    elif sort == "trending":
        base = base.where(Cluster.trending == True)
        order_clauses = [*newest_order, stats.c.post_count.desc()]
    else:  # "newest" (default)
        order_clauses = [*newest_order, stats.c.post_count.desc()]

    return await _materialize_discovery(base, order_clauses, limit, offset, uid, db)


@router.get("/trending")
async def get_trending_clusters(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Trending clusters by signal_score (dashboard intelligence feed)."""
    stats = _item_stats_subquery()
    try:
        result = await db.execute(
            select(Cluster, stats.c.post_count, ClusterSignal)
            .join(stats, stats.c.cluster_id == Cluster.id)
            .outerjoin(ClusterSignal, ClusterSignal.cluster_id == Cluster.id)
            .where(
                Cluster.trending == True,
                Cluster.pipeline_version == PIPELINE_VERSION,
            )
            .order_by(_signal_score_order().desc().nullslast())
            .limit(limit)
        )
        rows = result.all()
    except SQLAlchemyError:
        await db.rollback()
        logger.warning("cluster_signals unavailable for trending clusters; using legacy score order", exc_info=True)
        result = await db.execute(
            select(Cluster, stats.c.post_count)
            .join(stats, stats.c.cluster_id == Cluster.id)
            .where(
                Cluster.trending == True,
                Cluster.pipeline_version == PIPELINE_VERSION,
            )
            .order_by(Cluster.signal_score.desc().nullslast())
            .limit(limit)
        )
        rows = [(cluster, post_count, None) for cluster, post_count in result.all()]
    return {
        "data": [
            {
                "id": c.id,
                "name": c.name,
                "summary": c.summary,
                "trending": c.trending,
                "signalScore": _signal_score(c, signal),
                "postCount": post_count,
            }
            for c, post_count, signal in rows
        ]
    }


@router.get("/domains")
async def list_cluster_domains(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Distinct opportunity_domain values across cluster_items (Search filter source)."""
    result = await db.execute(
        select(ClusterItem.opportunity_domain)
        .where(
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            ClusterItem.opportunity_domain.isnot(None),
            func.trim(ClusterItem.opportunity_domain) != "",
        )
        .distinct()
        .order_by(ClusterItem.opportunity_domain.asc())
    )
    return {"data": [d for (d,) in result.all()]}


# ──────────────────────────────────────────────────────────────────────────
# Cluster detail + sub-resources
# ──────────────────────────────────────────────────────────────────────────

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
    """Problem-breakdown detail: problem_statement, solution_angle, top evidence,
    and a paginated list of the cluster's items."""
    cluster = await _fetch_cluster(cluster_id, db)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    total_posts = await count_cluster_items(db, cluster_id)
    top_items = await load_cluster_items(db, cluster_id, limit=TOP_EVIDENCE_COUNT)
    page_items = await load_cluster_items(db, cluster_id, limit=pageSize, offset=(page - 1) * pageSize)
    signal = await _signal_for_cluster(cluster_id, db)
    all_ids = [row[0] for row in (await db.execute(
        select(ClusterItem.id).where(
            ClusterItem.cluster_id == cluster_id,
            ClusterItem.pipeline_version == PIPELINE_VERSION,
        )
    )).all()]

    return {
        **_serialize_cluster(cluster, post_count=total_posts, signal=signal),
        "problemStatement": representative_problem_statement(top_items),
        "solutionAngle": (top_items[0].solution_angle if top_items else None),
        "topEvidence": [serialize_evidence_item(it) for it in top_items],
        "signals": {"totalPosts": total_posts},
        "postIds": all_ids,
        "posts": [serialize_evidence_item(it) for it in page_items],
        "page": page,
        "pageSize": pageSize,
        "totalPosts": total_posts,
    }


@router.get("/{cluster_id}/items")
async def get_cluster_items(
    cluster_id: int,
    page: int = Query(1, ge=1),
    pageSize: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Evidence list — the cluster's items for the full side-by-side post view."""
    if not await _fetch_cluster(cluster_id, db):
        raise HTTPException(status_code=404, detail="Cluster not found")

    total = await count_cluster_items(db, cluster_id)
    items = await load_cluster_items(db, cluster_id, limit=pageSize, offset=(page - 1) * pageSize)
    return {
        "clusterId": cluster_id,
        "page": page,
        "pageSize": pageSize,
        "total": total,
        "data": [serialize_evidence_item(it) for it in items],
    }


@router.get("/{cluster_id}/tasks")
async def get_cluster_tasks(
    cluster_id: int,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Task list — distinct proposed solution angles for the cluster, ranked by
    their best supporting post."""
    if not await _fetch_cluster(cluster_id, db):
        raise HTTPException(status_code=404, detail="Cluster not found")

    items = await load_cluster_items(db, cluster_id, limit=TASKS_SCAN_LIMIT)
    return {"clusterId": cluster_id, "data": distinct_solution_angles(items)}


@router.get("/{cluster_id}/snapshots")
async def get_cluster_snapshots(
    cluster_id: int,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    cluster = await _fetch_cluster(cluster_id, db)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    result = await db.execute(
        select(ClusterSnapshot)
        .where(ClusterSnapshot.cluster_id == cluster_id)
        .order_by(ClusterSnapshot.date.asc())
    )
    snapshots = result.scalars().all()
    signal = await _signal_for_cluster(cluster_id, db)

    return {
        "clusterId": cluster_id,
        "hasSufficientHistory": len(snapshots) >= SUFFICIENT_HISTORY_DAYS,
        "signalScore": _signal_score(cluster, signal),
        "snapshots": [
            {
                "date": s.date.isoformat() if s.date else None,
                "postCount": s.post_count,
                "avgComments": s.avg_comments,
                "samplePosts": s.sample_posts or [],
            }
            for s in snapshots
        ],
    }
