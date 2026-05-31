from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.auth import get_uid
from app.models import Cluster, ClusterSnapshot, Opportunity, ClusterMember, SourcePost
from app.limits import get_plan, ai_clusters_allowed, is_pro_plus, LimitReached
from app.brief import assemble_brief, new_meta, momentum_from_growth
from app.anthropic_client import generate_opportunity_brief, brief_model_version

router = APIRouter(prefix="/clusters", tags=["clusters"])

SUFFICIENT_HISTORY_DAYS = 7
BRIEF_TOP_POSTS = 8


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
        "growthRateMom": c.growth_rate_mom,
        "postVelocity": c.post_velocity,
        "growthAcceleration": c.growth_acceleration,
        "status": c.status,
        "description": c.description,
        "primaryDomain": c.primary_domain,
        "audienceType": c.audience_type,
        "problemCategory": c.problem_category,
        "monetizationCategory": c.monetization_category,
        "authorCount": c.author_count,
        "communityCount": c.community_count,
        "avgPainScore": c.avg_pain_score,
        "avgUrgencyScore": c.avg_urgency_score,
        "avgCommercialIntentScore": c.avg_commercial_intent_score,
        "avgSolutionSeekingScore": c.avg_solution_seeking_score,
        "revenuePotentialScore": c.revenue_potential_score,
        "competitionScore": c.competition_score,
        "saturationScore": c.saturation_score,
        "confidenceScore": c.confidence_score,
        "signals": signals,
    }


@router.get("/trending")
async def get_trending_clusters(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Top trending clusters by week-over-week growth (dashboard intelligence feed)."""
    # AI-detected clusters are a Pro feature (403 with limit_reached for free).
    plan = await get_plan(uid, db)
    if not ai_clusters_allowed(plan):
        raise LimitReached("clusters", plan, status_code=403)

    result = await db.execute(
        select(Cluster)
        .where(Cluster.trending == True)
        .order_by(Cluster.growth_rate_wow.desc().nullslast())
        .limit(limit)
    )
    clusters = list(result.scalars().all())

    cluster_ids = [c.id for c in clusters]
    snapshot_map: dict[int, int | None] = {}
    if cluster_ids:
        snapshots_result = await db.execute(
            select(ClusterSnapshot)
            .where(ClusterSnapshot.cluster_id.in_(cluster_ids))
            .distinct(ClusterSnapshot.cluster_id)
            .order_by(ClusterSnapshot.cluster_id, ClusterSnapshot.date.desc())
        )
        snapshot_map = {s.cluster_id: s.post_count for s in snapshots_result.scalars().all()}

    return {
        "data": [
            {
                "id": c.id,
                "name": c.name,
                "source": c.source,
                "signalScore": c.signal_score,
                "trending": c.trending,
                "growthRateWow": c.growth_rate_wow,
                "growthRateMom": c.growth_rate_mom,
                "postVelocity": c.post_velocity,
                "postCount": snapshot_map.get(c.id),
            }
            for c in clusters
        ]
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

    # All opportunity IDs in this cluster (for seeding user pipeline from system cluster)
    all_ids_result = await db.execute(
        select(Opportunity.id)
        .where(
            Opportunity.cluster_id == cluster_id,
            Opportunity.deleted_at == None,
        )
    )
    post_ids = [str(row[0]) for row in all_ids_result]

    return {
        **_serialize_cluster(cluster, post_count, signals),
        "postIds": post_ids,
        "posts": [
            {
                "id": p.id,
                "title": p.title,
                "description": p.description,
                "sourceUrl": p.source_url,
                "sourceScore": p.source_score,
                "signalScore": p.signal_score,
                "wtpSignal": p.wtp_signal,
                "intensity": p.intensity,
                "sentiment": p.sentiment,
                "domain": p.domain,
                "workaroundBoolean": p.workaround_boolean,
                "intensityScore": p.intensity_score,
                "sentimentScore": p.sentiment_score,
                "wtpConfidence": p.wtp_confidence,
                "workaroundConfidence": p.workaround_confidence,
                "createdAt": p.created_at,
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
        "growthRateMom": cluster.growth_rate_mom,
        "postVelocity": cluster.post_velocity,
        "snapshots": [
            {
                "date": s.date.isoformat(),
                "postCount": s.post_count,
                "samplePosts": s.sample_posts or [],
            }
            for s in snapshots
        ],
    }


@router.get("/{cluster_id}/members")
async def get_cluster_members(
    cluster_id: int,
    page: int = Query(1, ge=1),
    pageSize: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Posts belonging to a cluster via the universal cluster_members join."""
    offset = (page - 1) * pageSize
    result = await db.execute(
        select(ClusterMember, SourcePost)
        .join(SourcePost, ClusterMember.source_post_id == SourcePost.id, isouter=True)
        .where(ClusterMember.cluster_id == cluster_id)
        .order_by(ClusterMember.created_at.desc())
        .offset(offset)
        .limit(pageSize)
    )
    rows = result.all()
    total = await db.scalar(
        select(func.count()).select_from(ClusterMember).where(ClusterMember.cluster_id == cluster_id)
    )

    return {
        "clusterId": cluster_id,
        "page": page,
        "pageSize": pageSize,
        "total": total,
        "data": [
            {
                "id": m.id,
                "sourcePostId": m.source_post_id,
                "similarityScore": m.similarity_score,
                "membershipConfidence": m.membership_confidence,
                "assignedBy": m.assigned_by,
                "modelVersion": m.model_version,
                "post": {
                    "title": sp.title,
                    "url": sp.url,
                    "source": sp.source,
                    "sourceCommunityId": sp.source_community_id,
                    "score": sp.score,
                    "numComments": sp.num_comments,
                } if sp else None,
            }
            for m, sp in rows
        ],
    }
