from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.auth import get_uid
from app.models import Cluster, ClusterSnapshot, Opportunity

router = APIRouter(prefix="/clusters", tags=["clusters"])

SUFFICIENT_HISTORY_DAYS = 7


async def _get_signal_aggregations(cluster_id: int, db: AsyncSession) -> dict:
    """Compute signal distributions on-read from opportunities."""
    result = await db.execute(
        text("""
            SELECT
                COUNT(*)                                                        AS total,
                COUNT(*) FILTER (WHERE wtp_signal = true)                       AS wtp_count,
                COUNT(*) FILTER (WHERE workaround_boolean = true)               AS workaround_count,
                COUNT(*) FILTER (WHERE intensity = 'low')                       AS intensity_low,
                COUNT(*) FILTER (WHERE intensity = 'medium')                    AS intensity_medium,
                COUNT(*) FILTER (WHERE intensity = 'high')                      AS intensity_high,
                COUNT(*) FILTER (WHERE sentiment = 'positive')                  AS sentiment_positive,
                COUNT(*) FILTER (WHERE sentiment = 'negative')                  AS sentiment_negative,
                COUNT(*) FILTER (WHERE sentiment = 'neutral')                   AS sentiment_neutral
            FROM opportunities
            WHERE cluster_id = :cluster_id
              AND deleted_at IS NULL
        """),
        {"cluster_id": cluster_id},
    )
    row = result.mappings().one()
    total = row["total"] or 0

    def pct(n):
        return round((n / total) * 100, 1) if total > 0 else 0.0

    # Most common domain
    domain_result = await db.execute(
        text("""
            SELECT domain, COUNT(*) AS n
            FROM opportunities
            WHERE cluster_id = :cluster_id
              AND deleted_at IS NULL
              AND domain IS NOT NULL
            GROUP BY domain
            ORDER BY n DESC
            LIMIT 1
        """),
        {"cluster_id": cluster_id},
    )
    domain_row = domain_result.mappings().one_or_none()

    return {
        "totalPosts": total,
        "wtpRate": pct(row["wtp_count"]),
        "workaroundRate": pct(row["workaround_count"]),
        "intensity": {
            "low": pct(row["intensity_low"]),
            "medium": pct(row["intensity_medium"]),
            "high": pct(row["intensity_high"]),
        },
        "sentiment": {
            "positive": pct(row["sentiment_positive"]),
            "negative": pct(row["sentiment_negative"]),
            "neutral": pct(row["sentiment_neutral"]),
        },
        "topDomain": domain_row["domain"] if domain_row else None,
    }


async def _get_latest_post_count(cluster_id: int, db: AsyncSession) -> int | None:
    """Get post count from the most recent snapshot."""
    result = await db.execute(
        select(ClusterSnapshot.post_count)
        .where(ClusterSnapshot.cluster_id == cluster_id)
        .order_by(ClusterSnapshot.date.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _serialize_cluster(c: Cluster, post_count: int | None, signals: dict) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "source": c.source,
        "firstSeen": c.first_seen.isoformat() if c.first_seen else None,
        "postCount": post_count,
        "signalScore": c.signal_score,
        "trending": c.trending,
        "growthRateWow": c.growth_rate_wow,
        "signals": signals,
    }


@router.get("/{cluster_id}")
async def get_cluster(
    cluster_id: int,
    page: int = 1,
    pageSize: int = 20,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Cluster).where(Cluster.id == cluster_id)
    )
    cluster = result.scalar_one_or_none()

    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    signals = await _get_signal_aggregations(cluster_id, db)
    post_count = await _get_latest_post_count(cluster_id, db)

    # Paginated posts belonging to this cluster
    posts_query = (
        select(Opportunity)
        .where(
            Opportunity.cluster_id == cluster_id,
            Opportunity.deleted_at == None,
        )
        .order_by(Opportunity.source_score.desc().nullslast())
        .offset((page - 1) * pageSize)
        .limit(pageSize)
    )
    posts_result = await db.execute(posts_query)
    posts = posts_result.scalars().all()

    total_posts = await db.scalar(
        select(func.count())
        .where(
            Opportunity.cluster_id == cluster_id,
            Opportunity.deleted_at == None,
        )
    )

    return {
        **_serialize_cluster(cluster, post_count, signals),
        "posts": [
            {
                "id": p.id,
                "title": p.title,
                "sourceScore": p.source_score,
                "wtpSignal": p.wtp_signal,
                "intensity": p.intensity,
                "sentiment": p.sentiment,
                "domain": p.domain,
                "workaroundBoolean": p.workaround_boolean,
            }
            for p in posts
        ],
        "page": page,
        "pageSize": pageSize,
        "totalPosts": total_posts,
    }


@router.get("/{cluster_id}/snapshots")
async def get_cluster_snapshots(
    cluster_id: int,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    cluster_result = await db.execute(
        select(Cluster).where(Cluster.id == cluster_id)
    )
    cluster = cluster_result.scalar_one_or_none()

    cluster_exists = await db.scalar(
        select(func.count()).where(Cluster.id == cluster_id)
    )
    if not cluster_exists:
        raise HTTPException(status_code=404, detail="Cluster not found")

    result = await db.execute(
        select(ClusterSnapshot)
        .where(ClusterSnapshot.cluster_id == cluster_id)
        .order_by(ClusterSnapshot.date.asc())
    )
    snapshots = result.scalars().all()

    return {
        "clusterId": cluster_id,
        "hasSufficientHistory": len(snapshots) >= SUFFICIENT_HISTORY_DAYS,
        "growthRateWow": cluster.growth_rate_wow,
        "snapshots": [
            {
                "date": s.date.isoformat(),
                "postCount": s.post_count,
                "samplePosts": s.sample_posts or [],
            }
            for s in snapshots
        ],
    }