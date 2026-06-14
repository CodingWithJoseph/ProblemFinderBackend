"""Public, unauthenticated read-only endpoints for the marketing site.

Everything here is anonymous (no ``get_uid`` dependency) and filtered to the
current pipeline version. A cluster's posts are its ``cluster_items`` rows (the
simplified pipeline writes one flat ``cluster_items`` row per post).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PIPELINE_VERSION
from app.db import get_db
from app.models import Cluster, ClusterItem

router = APIRouter(prefix="/public", tags=["public"])

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
SAMPLE_POST_COUNT = 3
SEARCH_LIMIT = 20


def _add_cors(response: Response) -> None:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _serialize_post(it: ClusterItem) -> dict:
    return {
        "id": it.id,
        "title": it.title,
        "reddit_score": it.score,
        "subreddit": it.community,
    }


def _serialize_post_full(it: ClusterItem) -> dict:
    upvote_ratio = it.raw_json.get("upvote_ratio") if isinstance(it.raw_json, dict) else None
    return {
        **_serialize_post(it),
        "body": it.body,
        "url": it.url or it.permalink,
        "author": it.author,
        "comment_count": it.num_comments,
        "upvote_ratio": upvote_ratio,
        "problem_statement": it.problem_statement,
        "solution_angle": it.solution_angle,
        "posted_at": _iso(it.posted_at),
    }


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
        items.sort(key=lambda p: (p.score is not None, p.score or 0), reverse=True)
    return grouped


def _summarize_posts(items: list[ClusterItem]) -> dict:
    subreddits = sorted({it.community for it in items if it.community})
    dates = sorted(_iso(it.posted_at) for it in items if it.posted_at)
    return {
        "subreddits": subreddits,
        "date_range_start": dates[0] if dates else None,
        "date_range_end": dates[-1] if dates else None,
    }


def _serialize_cluster(c: Cluster, items: list[ClusterItem], sample_count: int) -> dict:
    summary = _summarize_posts(items)
    return {
        "id": c.id,
        "name": c.name,
        "summary": c.summary,
        "signal_score": c.signal_score,
        "post_count": len(items),
        "trending_status": "trending" if c.trending else None,
        "date_range_start": summary["date_range_start"],
        "date_range_end": summary["date_range_end"],
        "subreddits": summary["subreddits"],
        "sample_posts": [_serialize_post(it) for it in items[:sample_count]],
    }


@router.get("/clusters")
async def public_clusters(
    response: Response,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Top clusters by signal_score (v2 only)."""
    _add_cors(response)

    item_counts = (
        select(
            ClusterItem.cluster_id.label("cluster_id"),
            func.count().label("post_count"),
        )
        .where(
            ClusterItem.pipeline_version == PIPELINE_VERSION,
            ClusterItem.cluster_id.isnot(None),
        )
        .group_by(ClusterItem.cluster_id)
        .subquery()
    )

    query = (
        select(Cluster)
        .join(item_counts, item_counts.c.cluster_id == Cluster.id)
        .where(Cluster.pipeline_version == PIPELINE_VERSION)
        .order_by(Cluster.signal_score.desc().nullslast(), item_counts.c.post_count.desc())
        .offset(offset)
        .limit(limit)
    )

    clusters = list((await db.execute(query)).scalars().all())
    items_map = await _items_by_cluster([c.id for c in clusters], db)

    return {
        "data": [
            _serialize_cluster(c, items_map.get(c.id, []), SAMPLE_POST_COUNT)
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

    items = (await _items_by_cluster([cluster_id], db)).get(cluster_id, [])

    return {
        "data": {
            **_serialize_cluster(cluster, items, SAMPLE_POST_COUNT),
            "posts": [_serialize_post_full(it) for it in items],
        }
    }


@router.get("/search")
async def public_search(
    response: Response,
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    """Search clusters by name (v2 only). Returns clusters, not posts."""
    _add_cors(response)

    clusters = list((await db.execute(
        select(Cluster)
        .where(
            Cluster.name.ilike(f"%{q}%"),
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
        .limit(SEARCH_LIMIT)
    )).scalars().all())

    items_map = await _items_by_cluster([c.id for c in clusters], db)

    return {
        "data": [
            _serialize_cluster(c, items_map.get(c.id, []), SAMPLE_POST_COUNT)
            for c in clusters
        ]
    }


async def compute_stats(db: AsyncSession) -> dict:
    """Public aggregate counts for the marketing homepage (v2 only)."""
    item_count = await db.scalar(
        select(func.count())
        .select_from(ClusterItem)
        .where(ClusterItem.pipeline_version == PIPELINE_VERSION)
    ) or 0
    cluster_count = await db.scalar(
        select(func.count())
        .select_from(Cluster)
        .where(Cluster.pipeline_version == PIPELINE_VERSION)
    ) or 0

    return {
        "conversationsAnalyzed": item_count,
        "clustersDetected": cluster_count,
        "opportunitiesFound": item_count,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/stats")
async def public_stats(response: Response, db: AsyncSession = Depends(get_db)):
    _add_cors(response)
    return await compute_stats(db)
