"""Public, unauthenticated read-only endpoints for the marketing site.

Everything here is anonymous (no ``get_uid`` dependency) and filtered to the
current pipeline version. The "posts" in a cluster are the v2 source_posts
reached through cluster_members (the simplified pipeline writes source_posts +
cluster_members, not the legacy opportunities table).

NOTE ON FILE LOCATION: the job brief names this ``app/routers/public.py`` but
this codebase keeps all routers under ``app/routes/`` (see main.py). The module
is placed there for consistency; the route prefix is still ``/public``.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PIPELINE_VERSION
from app.db import get_db
from app.models import Cluster, ClusterMember, Opportunity, SourcePost

router = APIRouter(prefix="/public", tags=["public"])

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
SAMPLE_POST_COUNT = 3
SEARCH_LIMIT = 20


def _add_cors(response: Response) -> None:
    """Public endpoints are consumed by the marketing site from any origin."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _serialize_post(sp: SourcePost) -> dict:
    """A v2 source_post in public shape (reddit_score == source_posts.score,
    subreddit == source_community_id)."""
    return {
        "id": sp.id,
        "title": sp.title,
        "reddit_score": sp.score,
        "subreddit": sp.source_community_id,
    }


def _serialize_post_full(sp: SourcePost) -> dict:
    return {
        **_serialize_post(sp),
        "body": sp.body,
        "url": sp.url,
        "author": sp.source_author_id,
        "comment_count": sp.num_comments,
        "upvote_ratio": sp.upvote_ratio,
        "posted_at": _iso(sp.posted_at),
    }


def _trending_status(c: Cluster) -> str | None:
    if c.status:
        return c.status
    return "trending" if c.trending else None


async def _posts_by_cluster(cluster_ids: list[int], db: AsyncSession) -> dict[int, list[SourcePost]]:
    """Map cluster_id -> its v2 source_posts (highest reddit_score first)."""
    if not cluster_ids:
        return {}
    rows = (await db.execute(
        select(ClusterMember.cluster_id, SourcePost)
        .join(SourcePost, ClusterMember.source_post_id == SourcePost.id)
        .where(
            ClusterMember.cluster_id.in_(cluster_ids),
            ClusterMember.pipeline_version == PIPELINE_VERSION,
        )
    )).all()
    grouped: dict[int, list[SourcePost]] = {}
    for cid, sp in rows:
        grouped.setdefault(cid, []).append(sp)
    for posts in grouped.values():
        posts.sort(key=lambda p: (p.score is not None, p.score), reverse=True)
    return grouped


def _summarize_posts(posts: list[SourcePost]) -> dict:
    subreddits = sorted({p.source_community_id for p in posts if p.source_community_id})
    dates = sorted(_iso(p.posted_at) for p in posts if p.posted_at)
    return {
        "subreddits": subreddits,
        "date_range_start": dates[0] if dates else None,
        "date_range_end": dates[-1] if dates else None,
    }


def _serialize_cluster(c: Cluster, posts: list[SourcePost], sample_count: int) -> dict:
    summary = _summarize_posts(posts)
    return {
        "id": c.id,
        "name": c.name,
        "post_count": len(posts),
        "trending_status": _trending_status(c),
        "growth_rate": c.growth_rate_wow,
        "date_range_start": summary["date_range_start"],
        "date_range_end": summary["date_range_end"],
        "subreddits": summary["subreddits"],
        "is_singleton": bool(c.is_singleton),
        "sample_posts": [_serialize_post(p) for p in posts[:sample_count]],
    }


@router.get("/clusters")
async def public_clusters(
    response: Response,
    include_singletons: bool = Query(False),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Top clusters ordered by post_count desc (v2 only)."""
    _add_cors(response)

    # post_count is the number of v2 cluster_members; order/paginate on it.
    member_counts = (
        select(
            ClusterMember.cluster_id.label("cluster_id"),
            func.count().label("post_count"),
        )
        .where(ClusterMember.pipeline_version == PIPELINE_VERSION)
        .group_by(ClusterMember.cluster_id)
        .subquery()
    )

    query = (
        select(Cluster)
        .join(member_counts, member_counts.c.cluster_id == Cluster.id)
        .where(Cluster.pipeline_version == PIPELINE_VERSION)
    )
    if not include_singletons:
        query = query.where(Cluster.is_singleton == False)
    query = query.order_by(member_counts.c.post_count.desc()).offset(offset).limit(limit)

    clusters = list((await db.execute(query)).scalars().all())
    posts_map = await _posts_by_cluster([c.id for c in clusters], db)

    return {
        "data": [
            _serialize_cluster(c, posts_map.get(c.id, []), SAMPLE_POST_COUNT)
            for c in clusters
        ]
    }


@router.get("/clusters/{cluster_id}")
async def public_cluster_detail(
    cluster_id: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Full cluster detail plus all of its posts (v2 only)."""
    _add_cors(response)

    cluster = (await db.execute(
        select(Cluster).where(
            Cluster.id == cluster_id,
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
    )).scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    posts = (await _posts_by_cluster([cluster_id], db)).get(cluster_id, [])

    return {
        "data": {
            **_serialize_cluster(cluster, posts, SAMPLE_POST_COUNT),
            "description": cluster.description,
            "primary_domain": cluster.primary_domain,
            "posts": [_serialize_post_full(p) for p in posts],
        }
    }


@router.get("/search")
async def public_search(
    response: Response,
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    """Search clusters by name (v2 only). Returns clusters, not posts.

    hybrid_search() returns SETOF opportunities (individual posts), so it is not
    a fit for a clusters-only public search; we use an ILIKE match on the
    cluster name as specified.
    """
    _add_cors(response)

    clusters = list((await db.execute(
        select(Cluster)
        .where(
            Cluster.name.ilike(f"%{q}%"),
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
        .limit(SEARCH_LIMIT)
    )).scalars().all())

    posts_map = await _posts_by_cluster([c.id for c in clusters], db)

    return {
        "data": [
            _serialize_cluster(c, posts_map.get(c.id, []), SAMPLE_POST_COUNT)
            for c in clusters
        ]
    }


async def compute_stats(db: AsyncSession) -> dict:
    """Public aggregate counts for the marketing homepage (v2 only).

    Shared by GET /public/stats and the backwards-compatible /homepage/stats.
    """
    source_post_count = await db.scalar(
        select(func.count())
        .select_from(SourcePost)
        .where(SourcePost.pipeline_version == PIPELINE_VERSION)
    )
    opportunity_count = await db.scalar(
        select(func.count()).where(
            Opportunity.deleted_at == None,
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
    )
    cluster_count = await db.scalar(
        select(func.count())
        .select_from(Cluster)
        .where(Cluster.pipeline_version == PIPELINE_VERSION)
    )

    opportunities_found = opportunity_count or 0

    # highSignalOpportunities / wtpSignals were derived from the legacy
    # signal_score / wtp_signal columns the simplified pipeline no longer
    # writes (JOB 3), so they are no longer reported.
    return {
        "conversationsAnalyzed": source_post_count or opportunities_found,
        "clustersDetected": cluster_count or 0,
        "opportunitiesFound": opportunities_found,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/stats")
async def public_stats(response: Response, db: AsyncSession = Depends(get_db)):
    _add_cors(response)
    return await compute_stats(db)
