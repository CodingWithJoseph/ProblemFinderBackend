from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, text, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.auth import get_uid
from app.config import PIPELINE_VERSION
from app.models import Cluster, ClusterSnapshot, Opportunity, ClusterMember, SourcePost, Pipeline
from app.limits import get_plan, ai_clusters_allowed, is_pro_plus, LimitReached
from app.brief import assemble_brief, new_meta, momentum_from_growth
from app.anthropic_client import generate_opportunity_brief, brief_model_version

router = APIRouter(prefix="/clusters", tags=["clusters"])

SUFFICIENT_HISTORY_DAYS = 7
BRIEF_TOP_POSTS = 8

# --- Cluster search / browse (cluster-first discovery) ---------------------
SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 50
SEARCH_SAMPLE_POSTS = 3


def _iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _excerpt(title, description, limit: int = 240) -> str:
    title = (title or "").strip()
    body = (description or "").strip()
    joined = f"{title} - {body}" if body else title
    return joined[:limit].rstrip()


def _brief_response(cluster: Cluster) -> dict:
    return {
        "clusterId": cluster.id,
        "brief": cluster.opportunity_brief,
        "generatedAt": _iso(cluster.brief_generated_at) or None,
        "modelVersion": cluster.brief_model_version,
    }


def _build_brief_inputs(cluster: Cluster, posts: list[Opportunity]) -> tuple[dict, dict]:
    total = len(posts)

    dates = sorted(_iso(p.created_at) for p in posts if p.created_at)
    representative_posts = [
        {
            "source": p.source or "reddit",
            "excerpt": _excerpt(p.title, p.description),
            **({"url": p.source_url} if p.source_url else {}),
            **({"score": round(float(p.source_score), 1)} if p.source_score is not None else {}),
        }
        for p in posts[:BRIEF_TOP_POSTS]
    ]

    evidence = {
        "representativePosts": representative_posts,
        "totalPostsAnalyzed": total,
        "dateRange": {
            "from": dates[0] if dates else "",
            "to": dates[-1] if dates else "",
        },
    }
    # The simplified pipeline no longer produces pain/urgency/wtp scores; the
    # only quantitative signal left is post frequency.
    signal_strength = {
        "frequency": min(100, total),
    }
    return evidence, signal_strength


def _build_brief_context(
    cluster: Cluster,
    posts: list[Opportunity],
    evidence: dict,
) -> str:
    lines = [
        f"Cluster name: {cluster.name or 'Unnamed cluster'}",
        f"Total posts analyzed: {evidence['totalPostsAnalyzed']}",
        f"Growth week-over-week: {cluster.growth_rate_wow}",
        "",
        "Top representative posts (most upvoted first):",
    ]
    for i, p in enumerate(posts[:BRIEF_TOP_POSTS], 1):
        lines.append(f"{i}. [score {p.source_score}] {(p.title or '').strip()}")
        body = (p.description or "").strip()
        if body:
            lines.append(f"   {body[:300]}")
    return "\n".join(lines)


async def _get_signal_aggregations(cluster_id: int, db: AsyncSession) -> dict:
    """Post-count aggregation on-read from opportunities.

    The simplified pipeline no longer writes per-post scoring columns
    (wtp/workaround/intensity/sentiment/domain), so the on-read signal
    distribution is reduced to a post count.
    """
    result = await db.execute(
        text("""
            SELECT COUNT(*) AS total
            FROM opportunities
            WHERE cluster_id = :cluster_id
              AND deleted_at IS NULL
              AND pipeline_version = :pipeline_version
        """),
        {"cluster_id": cluster_id, "pipeline_version": PIPELINE_VERSION},
    )
    row = result.mappings().one()
    return {"totalPosts": row["total"] or 0}


async def _get_latest_post_count(cluster_id: int, db: AsyncSession) -> int | None:
    """Get post count from the most recent snapshot."""
    result = await db.execute(
        select(ClusterSnapshot.post_count)
        .where(
            ClusterSnapshot.cluster_id == cluster_id,
        )
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
        "trending": c.trending,
        "growthRateWow": c.growth_rate_wow,
        "growthRateMom": c.growth_rate_mom,
        "postVelocity": c.post_velocity,
        "growthAcceleration": c.growth_acceleration,
        "status": c.status,
        "description": c.description,
        "audienceType": c.audience_type,
        "problemCategory": c.problem_category,
        "monetizationCategory": c.monetization_category,
        "authorCount": c.author_count,
        "communityCount": c.community_count,
        "revenuePotentialScore": c.revenue_potential_score,
        "competitionScore": c.competition_score,
        "saturationScore": c.saturation_score,
        "signals": signals,
        "briefGeneratedAt": _iso(c.brief_generated_at) or None,
        "briefModelVersion": c.brief_model_version,
        "hasOpportunityBrief": c.opportunity_brief is not None,
    }


# ──────────────────────────────────────────────────────────────────────────
# Cluster-first discovery: search + browse
#
# These endpoints make the *cluster* the primary unit of search (posts live
# inside clusters). They read the v2 simplified pipeline tables — cluster_members
# joined to source_posts — which is where post_count, subreddits, date ranges and
# noise_filter_reason live (the legacy ``opportunities`` table has none of these).
# Clusters have no embedding column, so the optional pgvector cosine tiebreaker
# from the spec is skipped (documented decision).
# ──────────────────────────────────────────────────────────────────────────


async def _posts_by_cluster_v2(cluster_ids: list[int], db: AsyncSession) -> dict[int, list[SourcePost]]:
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
        posts.sort(key=lambda p: (p.score is not None, p.score or 0), reverse=True)
    return grouped


async def _watched_cluster_ids(uid: str, cluster_ids: list[int], db: AsyncSession) -> set[int]:
    """Set of the given cluster ids the user is currently watching.

    A cluster is "watched" when the user has a non-removed pipeline card whose
    ``source_cluster_id`` (stored as text) equals the cluster id.
    """
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


def _sample_post(sp: SourcePost) -> dict:
    return {
        "id": sp.id,
        "title": sp.title,
        "subreddit": sp.source_community_id,
        "reddit_score": sp.score,
        "noise_filter_reason": sp.noise_filter_reason,
    }


def _derive_top_signal(posts: list[SourcePost]) -> str | None:
    """Most common non-empty noise_filter_reason across the given (top) posts."""
    reasons = [p.noise_filter_reason for p in posts if p.noise_filter_reason]
    if not reasons:
        return None
    return Counter(reasons).most_common(1)[0][0]


def _serialize_discovery_cluster(
    c: Cluster, posts: list[SourcePost], watched_ids: set[int]
) -> dict:
    top_posts = posts[:SEARCH_SAMPLE_POSTS]
    subreddits = sorted({p.source_community_id for p in posts if p.source_community_id})
    dates = sorted(_iso(p.posted_at) for p in posts if p.posted_at)
    return {
        "id": c.id,
        "name": c.name,
        "post_count": len(posts),
        "trending_status": c.status if c.status else ("trending" if c.trending else None),
        "growth_rate": c.growth_rate_wow,
        "date_range_start": dates[0] if dates else None,
        "date_range_end": dates[-1] if dates else None,
        "subreddits": subreddits,
        "is_singleton": bool(c.is_singleton),
        "top_signal": _derive_top_signal(top_posts),
        "sample_posts": [_sample_post(p) for p in top_posts],
        "is_watched": c.id in watched_ids,
    }


def _member_stats_subquery():
    """Per-cluster post_count + most-recent post date over the v2 pipeline."""
    return (
        select(
            ClusterMember.cluster_id.label("cluster_id"),
            func.count().label("post_count"),
            func.max(SourcePost.posted_at).label("last_posted"),
        )
        .join(SourcePost, ClusterMember.source_post_id == SourcePost.id)
        .where(ClusterMember.pipeline_version == PIPELINE_VERSION)
        .group_by(ClusterMember.cluster_id)
        .subquery()
    )


async def _materialize_discovery(
    base, order_clauses: list, limit: int, offset: int, uid: str, db: AsyncSession
) -> dict:
    """Shared tail for search/browse: count, page, hydrate posts + watched state."""
    total = await db.scalar(select(func.count()).select_from(base.subquery()))

    query = base.order_by(*order_clauses).offset(offset).limit(limit)
    clusters = list((await db.execute(query)).scalars().all())

    cluster_ids = [c.id for c in clusters]
    posts_map = await _posts_by_cluster_v2(cluster_ids, db)
    watched_ids = await _watched_cluster_ids(uid, cluster_ids, db)

    return {
        "data": [
            _serialize_discovery_cluster(c, posts_map.get(c.id, []), watched_ids)
            for c in clusters
        ],
        "total": total or 0,
    }


@router.get("/search")
async def search_clusters(
    q: str = Query(..., min_length=2),
    limit: int = Query(SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    min_posts: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Cluster-first search. Matches clusters by name (full phrase + per-token
    ILIKE), ordered by post_count desc with recency as tiebreaker."""
    conditions = [Cluster.name.ilike(f"%{q}%")]
    for token in (t for t in q.split() if len(t) >= 2):
        conditions.append(Cluster.name.ilike(f"%{token}%"))

    member_stats = _member_stats_subquery()
    base = (
        select(Cluster)
        .join(member_stats, member_stats.c.cluster_id == Cluster.id)
        .where(
            Cluster.pipeline_version == PIPELINE_VERSION,
            Cluster.is_singleton == False,
            member_stats.c.post_count >= min_posts,
            or_(*conditions),
        )
    )
    order_clauses = [
        member_stats.c.post_count.desc(),
        member_stats.c.last_posted.desc().nullslast(),
    ]
    return await _materialize_discovery(base, order_clauses, limit, offset, uid, db)


@router.get("/browse")
async def browse_clusters(
    limit: int = Query(SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    sort: str = Query("largest"),
    subreddit: str | None = Query(None),
    min_posts: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Default (no-query) cluster discovery feed with sort + subreddit filter."""
    member_stats = _member_stats_subquery()
    base = (
        select(Cluster)
        .join(member_stats, member_stats.c.cluster_id == Cluster.id)
        .where(
            Cluster.pipeline_version == PIPELINE_VERSION,
            Cluster.is_singleton == False,
            member_stats.c.post_count >= min_posts,
        )
    )

    if subreddit:
        member_exists = (
            select(ClusterMember.id)
            .join(SourcePost, ClusterMember.source_post_id == SourcePost.id)
            .where(
                ClusterMember.cluster_id == Cluster.id,
                ClusterMember.pipeline_version == PIPELINE_VERSION,
                SourcePost.source_community_id == subreddit,
            )
        )
        base = base.where(member_exists.exists())

    if sort == "newest":
        order_clauses = [member_stats.c.last_posted.desc().nullslast()]
    elif sort == "trending":
        order_clauses = [Cluster.growth_rate_wow.desc().nullslast()]
    else:  # "largest" (default)
        order_clauses = [member_stats.c.post_count.desc()]

    return await _materialize_discovery(base, order_clauses, limit, offset, uid, db)


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
        .where(
            Cluster.trending == True,
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
        .order_by(Cluster.growth_rate_wow.desc().nullslast())
        .limit(limit)
    )
    clusters = list(result.scalars().all())

    cluster_ids = [c.id for c in clusters]
    snapshot_map: dict[int, int | None] = {}
    if cluster_ids:
        snapshots_result = await db.execute(
            select(ClusterSnapshot)
            .where(
                ClusterSnapshot.cluster_id.in_(cluster_ids),
            )
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
        select(Cluster).where(
            Cluster.id == cluster_id,
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
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
            Opportunity.pipeline_version == PIPELINE_VERSION,
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
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
    )

    # All opportunity IDs in this cluster (for seeding user pipeline from system cluster)
    all_ids_result = await db.execute(
        select(Opportunity.id)
        .where(
            Opportunity.cluster_id == cluster_id,
            Opportunity.deleted_at == None,
            Opportunity.pipeline_version == PIPELINE_VERSION,
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
                "createdAt": p.created_at,
            }
            for p in posts
        ],
        "page": page,
        "pageSize": pageSize,
        "totalPosts": total_posts,
    }


@router.get("/{cluster_id}/brief")
async def get_cluster_brief(
    cluster_id: int,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Cluster).where(
            Cluster.id == cluster_id,
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
    )
    cluster = result.scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return _brief_response(cluster)


@router.post("/{cluster_id}/brief/refresh")
async def refresh_cluster_brief(
    cluster_id: int,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    plan = await get_plan(uid, db)
    if not is_pro_plus(plan):
        raise LimitReached("opportunity_brief", plan, status_code=403)

    result = await db.execute(
        select(Cluster).where(
            Cluster.id == cluster_id,
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
    )
    cluster = result.scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    posts_result = await db.execute(
        select(Opportunity)
        .where(
            Opportunity.cluster_id == cluster_id,
            Opportunity.deleted_at == None,
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
        .order_by(Opportunity.source_score.desc().nullslast())
        .limit(500)
    )
    posts = list(posts_result.scalars().all())

    snapshot_result = await db.execute(
        select(ClusterSnapshot)
        .where(
            ClusterSnapshot.cluster_id == cluster_id,
        )
        .order_by(ClusterSnapshot.date.desc())
        .limit(1)
    )
    snapshot = snapshot_result.scalar_one_or_none()

    evidence, signal_strength = _build_brief_inputs(cluster, posts)
    context = _build_brief_context(cluster, posts, evidence)

    content = await generate_opportunity_brief(context)
    if content is None:
        raise HTTPException(status_code=502, detail="Could not generate opportunity brief")

    if cluster.growth_acceleration is not None or cluster.growth_rate_wow is not None:
        content["momentum"] = momentum_from_growth(cluster.growth_acceleration, cluster.growth_rate_wow)

    model = brief_model_version()
    meta = new_meta(
        cluster.id,
        snapshot.id if snapshot else "",
        model,
        refreshed=cluster.opportunity_brief is not None,
    )
    brief = assemble_brief(content, meta=meta, evidence=evidence, signal_strength=signal_strength)
    generated_at = datetime.fromisoformat(meta["generatedAt"])

    cluster.opportunity_brief = brief
    cluster.brief_generated_at = generated_at
    cluster.brief_model_version = model

    await db.commit()
    return _brief_response(cluster)


@router.get("/{cluster_id}/snapshots")
async def get_cluster_snapshots(
    cluster_id: int,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    cluster_result = await db.execute(
        select(Cluster).where(
            Cluster.id == cluster_id,
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
    )
    cluster = cluster_result.scalar_one_or_none()

    cluster_exists = await db.scalar(
        select(func.count()).where(
            Cluster.id == cluster_id,
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
    )
    if not cluster_exists:
        raise HTTPException(status_code=404, detail="Cluster not found")

    result = await db.execute(
        select(ClusterSnapshot)
        .where(
            ClusterSnapshot.cluster_id == cluster_id,
        )
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
        .where(
            ClusterMember.cluster_id == cluster_id,
            ClusterMember.pipeline_version == PIPELINE_VERSION,
        )
        .order_by(ClusterMember.created_at.desc())
        .offset(offset)
        .limit(pageSize)
    )
    rows = result.all()
    total = await db.scalar(
        select(func.count())
        .select_from(ClusterMember)
        .where(
            ClusterMember.cluster_id == cluster_id,
            ClusterMember.pipeline_version == PIPELINE_VERSION,
        )
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
