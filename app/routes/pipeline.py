import csv
import io
import logging
from datetime import datetime, timezone
from typing import Literal, Optional
import math
import random
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Response
from sqlalchemy import select, text, update, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db import get_db, AsyncSessionLocal
from app.auth import get_uid
from app.config import PIPELINE_VERSION
from app.models import Pipeline, Opportunity, Cluster, ClusterMember, SourcePost
from app.limits import get_plan, limit_for, LimitReached
from app.routes.clusters import get_discovery_brief
from app.signal_overview import serialize_signal_overview

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

PATCHABLE_FIELDS = {
    "stage", "kill_criteria", "distribution_channels", "concept_angles",
    "exit_checklist", "notes", "url", "category", "revenue_model", "status", "name",
    "outcome", "mrr",
}

CAMEL_TO_SNAKE = {
    "killCriteria": "kill_criteria",
    "distributionChannels": "distribution_channels",
    "conceptAngles": "concept_angles",
    "exitChecklist": "exit_checklist",
    "revenueModel": "revenue_model",
    "stage": "stage",
    "notes": "notes",
    "url": "url",
    "category": "category",
    "status": "status",
    "name": "name",
}

# Canonical pipeline stages, in progression order. "watching" is the backlog —
# clusters the user is monitoring but not actively pursuing — and is the first
# stage, before "discovery". Terminal states (launched / archived) are tracked
# via launched_at / removed_at rather than as stage values.
PIPELINE_STAGES = ("watching", "discovery", "exploring", "validating", "building")
VALID_STAGES = frozenset(PIPELINE_STAGES)

MAX_COHERENCE_POSTS = 50

EXPORT_CSV_COLUMNS = (
    "pipeline_id",
    "pipeline_name",
    "pipeline_stage",
    "source_cluster_id",
    "cluster_id",
    "cluster_name",
    "post_id",
    "title",
    "description",
    "source",
    "source_url",
    "author",
    "source_score",
    "num_comments",
    "created_at",
)


def _format_cluster_metrics(raw: dict | None) -> dict | None:
    if not raw:
        return None
    return {
        "avgSourceScore": raw.get("avg_source_score"),
        "coherenceScore": raw.get("coherence_score"),
        "postCount": raw.get("post_count"),
        "computedAt": raw.get("computed_at"),
    }


def _serialize(p: Pipeline, posts: list[dict] | None = None) -> dict:
    return {
        "id": p.id,
        "userId": p.user_id,
        "name": p.name,
        "postIds": p.post_ids or [],
        "sourceClusterId": p.source_cluster_id,
        "stage": p.stage,
        "killCriteria": p.kill_criteria,
        "distributionChannels": p.distribution_channels or [],
        "conceptAngles": p.concept_angles or [],
        "exitChecklist": p.exit_checklist,
        "notes": p.notes,
        "url": p.url,
        "category": p.category,
        "revenueModel": p.revenue_model,
        "status": p.status,
        "clusterMetrics": _format_cluster_metrics(p.cluster_metrics),
        "outcome": p.outcome,
        "mrr": p.mrr,
        "outcomeNotedAt": p.outcome_noted_at.isoformat() if p.outcome_noted_at else None,
        "launchedAt": p.launched_at.isoformat() if p.launched_at else None,
        "removedAt": p.removed_at.isoformat() if p.removed_at else None,
        "createdAt": p.created_at.isoformat() if p.created_at else None,
        "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
        "posts": posts or [],
    }


def _serialize_signal_post(o: Opportunity) -> dict:
    return {
        "id": o.id,
        "title": o.title,
        "description": o.description,
        "source": o.source,
        "sourceUrl": o.source_url,
        "author": o.author,
        "sourceScore": o.source_score,
        "numComments": o.num_comments,
        "createdAt": o.created_at.isoformat() if o.created_at else None,
    }


def _avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _serialize_signal_cluster(cluster: Cluster | None) -> dict | None:
    if not cluster:
        return None
    return {
        "id": cluster.id,
        "name": cluster.name,
        "trending": cluster.trending,
        "growthRateWow": cluster.growth_rate_wow,
        "growthRateMom": cluster.growth_rate_mom,
        "postVelocity": cluster.post_velocity,
    }


def _build_signal_metrics(posts: list[Opportunity]) -> dict:
    count = len(posts)
    return {
        "postCount": count,
        "avgSourceScore": _avg([p.source_score for p in posts]),
    }


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value else None


def _cluster_id_from_card(card: Pipeline) -> int | None:
    try:
        return int(card.source_cluster_id) if card.source_cluster_id else None
    except (TypeError, ValueError):
        return None


async def _get_active_pipeline_card(pipeline_id: str, db: AsyncSession, uid: str) -> Pipeline:
    result = await db.execute(
        select(Pipeline).where(
            Pipeline.id == pipeline_id,
            Pipeline.user_id == uid,
            Pipeline.launched_at == None,
            Pipeline.removed_at == None,
        )
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Active pipeline card not found")
    return card


def _serialize_source_signal_post(member: ClusterMember, post: SourcePost) -> dict:
    observed_at = post.posted_at or post.scraped_at
    return {
        "id": post.id,
        "title": post.title or "Untitled post",
        "description": post.body,
        "source": post.source_community_id or post.source,
        "sourceUrl": post.url or post.permalink,
        "author": post.source_author_id,
        "sourceScore": post.score,
        "numComments": post.num_comments,
        "createdAt": _iso_or_none(observed_at),
        "similarityScore": member.similarity_score,
    }


def _serialize_signal_cluster_workspace(
    cluster: Cluster,
    rows: list[tuple[ClusterMember, SourcePost]],
) -> dict:
    posts = [post for _, post in rows]
    dates = sorted(
        observed
        for post in posts
        if (observed := _iso_or_none(post.posted_at or post.scraped_at))
    )
    return {
        **_serialize_signal_cluster(cluster),
        "summary": cluster.summary,
        "opportunityType": cluster.opportunity_type,
        "opportunityDomain": cluster.opportunity_domain,
        "postCount": len(posts),
        "commentCount": sum(post.num_comments or 0 for post in posts),
        "averageUpvoteRatio": _avg([post.upvote_ratio for post in posts]),
        "persistenceScore": cluster.persistence_score,
        "intraClusterDensity": cluster.intra_cluster_density,
        "silhouetteScore": cluster.silhouette_score,
        "authorCount": cluster.author_count,
        "communityCount": cluster.community_count,
        "growthAcceleration": cluster.growth_acceleration,
        "status": cluster.status,
        "dateRange": {
            "from": dates[0] if dates else None,
            "to": dates[-1] if dates else None,
        },
    }


def _serialize_workspace_metrics(rows: list[tuple[ClusterMember, SourcePost]]) -> dict:
    posts = [post for _, post in rows]
    return {
        "postCount": len(posts),
        "avgSourceScore": _avg([post.score for post in posts]),
        "commentCount": sum(post.num_comments or 0 for post in posts),
        "averageUpvoteRatio": _avg([post.upvote_ratio for post in posts]),
    }


def _serialize_evidence_summary(member: ClusterMember | None, post: SourcePost | Opportunity) -> dict:
    if isinstance(post, SourcePost):
        observed_at = post.posted_at or post.scraped_at
        body = post.body
        return {
            "id": post.id,
            "title": post.title or "Untitled post",
            "excerpt": (body or "")[:300],
            "community": post.source_community_id or post.source,
            "source": post.source,
            "engagement": post.score,
            "numComments": post.num_comments,
            "upvoteRatio": post.upvote_ratio,
            "similarityScore": member.similarity_score if member else None,
            "observedAt": _iso_or_none(observed_at),
            "dateType": "posted" if post.posted_at else "observed",
            "url": post.url or post.permalink,
        }

    return {
        "id": post.id,
        "title": post.title or "Untitled post",
        "excerpt": (post.description or "")[:300],
        "community": post.source,
        "source": post.source,
        "engagement": post.source_score,
        "numComments": post.num_comments,
        "upvoteRatio": post.upvote_ratio,
        "similarityScore": None,
        "observedAt": _iso_or_none(post.created_at),
        "dateType": "observed",
        "url": post.source_url,
    }


def _serialize_evidence_detail(member: ClusterMember | None, post: SourcePost | Opportunity) -> dict:
    summary = _serialize_evidence_summary(member, post)
    if isinstance(post, SourcePost):
        return {
            **summary,
            "body": post.body,
            "author": post.source_author_id,
            "topComments": post.top_comments or [],
        }
    return {
        **summary,
        "body": post.description,
        "author": post.author,
        "topComments": post.top_comments or [],
    }


async def _load_pipeline_signal_data(
    pipeline_id: str,
    db: AsyncSession,
    uid: str,
) -> tuple[Pipeline, dict | None, dict, list[Opportunity]]:
    result = await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    # source_cluster_id is stored as text; system cluster ids are integers.
    cluster = None
    cluster_int_id: int | None = None
    if card.source_cluster_id:
        try:
            cluster_int_id = int(card.source_cluster_id)
        except (TypeError, ValueError):
            cluster_int_id = None

    if cluster_int_id is not None:
        cluster_result = await db.execute(
            select(Cluster).where(
                Cluster.id == cluster_int_id,
                Cluster.pipeline_version == PIPELINE_VERSION,
            )
        )
        cluster = cluster_result.scalar_one_or_none()

    # Full cluster view: ALL posts in the cluster; fall back to the card's post_ids.
    if cluster_int_id is not None:
        posts_result = await db.execute(
            select(Opportunity)
            .where(
                Opportunity.cluster_id == cluster_int_id,
                Opportunity.deleted_at == None,
                Opportunity.pipeline_version == PIPELINE_VERSION,
            )
            .order_by(Opportunity.source_score.desc().nullslast())
        )
        posts = list(posts_result.scalars().all())
    elif card.post_ids:
        posts_result = await db.execute(
            select(Opportunity)
            .where(
                Opportunity.id.in_(card.post_ids),
                Opportunity.deleted_at == None,
                Opportunity.pipeline_version == PIPELINE_VERSION,
            )
            .order_by(Opportunity.source_score.desc().nullslast())
        )
        posts = list(posts_result.scalars().all())
    else:
        posts = []

    return card, _serialize_signal_cluster(cluster), _build_signal_metrics(posts), posts


def _serialize_pipeline_signal_payload(
    card: Pipeline,
    cluster_payload: dict | None,
    metrics: dict,
    posts: list[Opportunity],
) -> dict:
    return {
        "pipeline": {
            "id": card.id,
            "name": card.name,
            "notes": card.notes,
            "sourceClusterId": card.source_cluster_id,
            "stage": card.stage,
        },
        "cluster": cluster_payload,
        "metrics": metrics,
        "posts": [_serialize_signal_post(p) for p in posts],
    }


def _csv_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _build_signal_csv(card: Pipeline, cluster_payload: dict | None, posts: list[Opportunity]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=EXPORT_CSV_COLUMNS)
    writer.writeheader()

    for post in posts:
        writer.writerow({
            "pipeline_id": card.id,
            "pipeline_name": card.name,
            "pipeline_stage": card.stage,
            "source_cluster_id": card.source_cluster_id,
            "cluster_id": cluster_payload.get("id") if cluster_payload else None,
            "cluster_name": cluster_payload.get("name") if cluster_payload else None,
            "post_id": post.id,
            "title": post.title,
            "description": post.description,
            "source": post.source,
            "source_url": post.source_url,
            "author": post.author,
            "source_score": post.source_score,
            "num_comments": post.num_comments,
            "created_at": _csv_value(post.created_at),
        })

    return output.getvalue()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def _parse_embedding(raw) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    s = str(raw).strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return None
        return [float(x) for x in inner.split(",")]
    return None


async def _compute_and_write_metrics(pipeline_id: str, post_ids: list[str]) -> None:
    """Background task: compute cluster metrics and write them back to the pipeline row."""
    if not post_ids:
        return

    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                select(
                    Opportunity.id,
                    Opportunity.source_score,
                ).where(
                    Opportunity.id.in_(post_ids),
                    Opportunity.pipeline_version == PIPELINE_VERSION,
                )
            )
            rows = result.all()

            if not rows:
                return

            count = len(rows)
            scores = [r.source_score for r in rows if r.source_score is not None]
            avg_source_score = round(sum(scores) / len(scores), 2) if scores else None

            # Coherence score: average pairwise cosine similarity using embeddings
            coherence_score: float | None = None
            if count == 1:
                coherence_score = 1.0
            else:
                sample = post_ids if count <= MAX_COHERENCE_POSTS else random.sample(post_ids, MAX_COHERENCE_POSTS)
                emb_result = await db.execute(
                    text(
                        "SELECT id::text, embedding::text AS emb "
                        "FROM opportunities "
                        "WHERE id::text = ANY(:ids) AND embedding IS NOT NULL "
                        "AND pipeline_version = :pipeline_version"
                    ),
                    {"ids": sample, "pipeline_version": PIPELINE_VERSION},
                )
                emb_rows = emb_result.all()
                parsed = [_parse_embedding(r[1]) for r in emb_rows]
                vecs = [v for v in parsed if v is not None]

                if len(vecs) >= 2:
                    magnitudes = [math.sqrt(sum(x * x for x in v)) for v in vecs]
                    similarities: list[float] = []
                    for i in range(len(vecs)):
                        for j in range(i + 1, len(vecs)):
                            mag_i, mag_j = magnitudes[i], magnitudes[j]
                            if mag_i == 0.0 or mag_j == 0.0:
                                continue
                            dot = sum(x * y for x, y in zip(vecs[i], vecs[j]))
                            similarities.append(dot / (mag_i * mag_j))
                    if similarities:
                        coherence_score = round(sum(similarities) / len(similarities), 4)
                elif len(vecs) == 1:
                    coherence_score = 1.0

            metrics = {
                "avg_source_score": avg_source_score,
                "coherence_score": coherence_score,
                "post_count": count,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            }

            pipeline_result = await db.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
            pipeline = pipeline_result.scalar_one_or_none()
            if pipeline:
                pipeline.cluster_metrics = metrics
                await db.commit()
        except Exception:
            # Background task — don't surface to the user, but don't go blind.
            logger.exception("cluster_metrics computation failed for pipeline %s", pipeline_id)


async def _fetch_posts_for_cards(cards: list[Pipeline], db: AsyncSession) -> dict[str, list[dict]]:
    """Batch-fetch {id, title, sourceScore} for all post_ids across the given pipeline cards."""
    all_ids = list({pid for c in cards for pid in (c.post_ids or [])})
    if not all_ids:
        return {str(c.id): [] for c in cards}

    result = await db.execute(
        select(Opportunity.id, Opportunity.title, Opportunity.source_score)
        .where(
            Opportunity.id.in_(all_ids),
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
    )
    post_map: dict[str, dict] = {
        str(row.id): {"id": str(row.id), "title": row.title, "sourceScore": row.source_score}
        for row in result
    }

    return {
        str(c.id): [post_map[pid] for pid in (c.post_ids or []) if pid in post_map]
        for c in cards
    }


async def _update_opportunity_counts(opportunity_id: str, action: str, db: AsyncSession) -> None:
    result = await db.execute(
        select(Opportunity).where(
            Opportunity.id == opportunity_id,
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
    )
    opp = result.scalar_one_or_none()
    if not opp:
        return
    # Atomic counter updates so concurrent add/remove/launch can't lose writes.
    pcount = func.coalesce(Opportunity.pipeline_count, 0)
    if action == "add":
        values = {"pipeline_count": pcount + 1}
    elif action == "remove":
        values = {"pipeline_count": func.greatest(0, pcount - 1)}
    elif action == "launch":
        values = {
            "pipeline_count": func.greatest(0, pcount - 1),
            "launch_count": func.coalesce(Opportunity.launch_count, 0) + 1,
        }
    else:
        return
    await db.execute(
        update(Opportunity)
        .where(
            Opportunity.id == opportunity_id,
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
        .values(**values)
    )


class CreatePipelineBody(BaseModel):
    name: str
    post_ids: list[str]
    source_cluster_id: Optional[str] = None


class PatchPipelineBody(BaseModel):
    stage: Optional[str] = None
    kill_criteria: Optional[str] = None
    distribution_channels: Optional[list[str]] = None
    concept_angles: Optional[list] = None
    exit_checklist: Optional[dict] = None
    notes: Optional[str] = None
    url: Optional[str] = None
    category: Optional[str] = None
    revenue_model: Optional[str] = None
    status: Optional[str] = None
    name: Optional[str] = None
    outcome: Optional[str] = None
    mrr: Optional[float] = None
    post_ids: Optional[list[str]] = None


class LaunchBody(BaseModel):
    product_name: str


class WatchBody(BaseModel):
    cluster_id: str


@router.get("")
async def get_pipeline(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.launched_at == None,
            Pipeline.removed_at == None,
        ).order_by(Pipeline.created_at.desc())
    )
    cards = result.scalars().all()
    posts_map = await _fetch_posts_for_cards(list(cards), db)
    return {"data": [_serialize(c, posts=posts_map.get(str(c.id), [])) for c in cards]}


async def _find_watch_card(uid: str, cluster_id: str, db: AsyncSession) -> Pipeline | None:
    """The user's active (non-removed) pipeline card watching this cluster, if any."""
    return (await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.source_cluster_id == str(cluster_id),
            Pipeline.removed_at == None,
        )
    )).scalar_one_or_none()


@router.post("/watch")
async def watch_cluster(
    body: WatchBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Add a cluster to the user's pipeline at the Watching stage.

    Watching a cluster is modeled as a pipeline card whose ``source_cluster_id``
    points at the cluster and whose stage is ``watching`` (the backlog). The card
    is seeded with the cluster's opportunity ids so the Signal workspace can load
    its posts. Idempotent: re-watching returns the existing card.
    """
    existing = await _find_watch_card(uid, body.cluster_id, db)
    if existing:
        return {"is_watched": True, "pipeline_id": str(existing.id)}

    try:
        cluster_int_id = int(body.cluster_id)
    except (TypeError, ValueError):
        cluster_int_id = None

    cluster = None
    if cluster_int_id is not None:
        cluster = (await db.execute(
            select(Cluster).where(
                Cluster.id == cluster_int_id,
                Cluster.pipeline_version == PIPELINE_VERSION,
            )
        )).scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    # Free plan caps active pipeline cards — watching consumes the same resource
    # as creating a pipeline card, so enforce the same limit.
    active_cards = (await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.launched_at == None,
            Pipeline.removed_at == None,
        )
    )).scalars().all()
    plan = await get_plan(uid, db)
    cap = limit_for(plan, "max_pipeline_cards")
    if cap is not None and len(active_cards) >= cap:
        raise LimitReached("pipeline", plan, status_code=403)

    post_id_rows = (await db.execute(
        select(Opportunity.id).where(
            Opportunity.cluster_id == cluster_int_id,
            Opportunity.deleted_at == None,
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
    )).all()
    post_ids = [str(r[0]) for r in post_id_rows]

    card = Pipeline(
        user_id=uid,
        name=cluster.name or f"Cluster {body.cluster_id}",
        post_ids=post_ids,
        source_cluster_id=str(body.cluster_id),
        stage="watching",
    )
    db.add(card)
    await db.commit()
    await db.refresh(card)
    return {"is_watched": True, "pipeline_id": str(card.id)}


@router.delete("/watch/{cluster_id}")
async def unwatch_cluster(
    cluster_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Remove a cluster from the user's pipeline (soft-delete the watch card)."""
    card = await _find_watch_card(uid, cluster_id, db)
    if card:
        now = datetime.now(timezone.utc)
        card.removed_at = now
        card.updated_at = now
        await db.commit()
    return {"is_watched": False}


@router.get("/{pipeline_id}/signal")
async def get_pipeline_signal(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Real-data Signal workspace anchored to an active user pipeline card.

    The top-level signalOverview field passes through the AI-generated JSONB
    document unchanged. Its overview* metadata fields describe generation
    state and remain present when no overview is available.
    """
    card = await _get_active_pipeline_card(pipeline_id, db, uid)
    cluster_id = _cluster_id_from_card(card)

    cluster = None
    member_rows: list[tuple[ClusterMember, SourcePost]] = []
    if cluster_id is not None:
        cluster = (await db.execute(
            select(Cluster).where(
                Cluster.id == cluster_id,
                Cluster.pipeline_version == PIPELINE_VERSION,
            )
        )).scalar_one_or_none()
        if cluster:
            member_rows = list((await db.execute(
                select(ClusterMember, SourcePost)
                .join(SourcePost, ClusterMember.source_post_id == SourcePost.id)
                .where(
                    ClusterMember.cluster_id == cluster_id,
                    ClusterMember.pipeline_version == PIPELINE_VERSION,
                    SourcePost.is_deleted.isnot(True),
                    SourcePost.is_removed.isnot(True),
                )
                .order_by(SourcePost.score.desc().nullslast())
            )).all())

    if cluster:
        cluster_payload = _serialize_signal_cluster_workspace(cluster, member_rows)
        metrics = _serialize_workspace_metrics(member_rows)
        posts = [_serialize_source_signal_post(member, post) for member, post in member_rows]
        analytics = {
            "sourceBreakdown": cluster.source_breakdown or [],
            "topTerms": cluster.top_tfidf_terms or [],
            "postVolumeByDate": cluster.post_volume_by_date or [],
        }
        return {
            "pipeline": {
                "id": card.id,
                "name": card.name,
                "notes": card.notes,
                "sourceClusterId": card.source_cluster_id,
                "stage": card.stage,
            },
            "cluster": cluster_payload,
            "metrics": metrics,
            "analytics": analytics,
            "availability": {"clusterAnalytics": True, "brief": True},
            "posts": posts,
            **serialize_signal_overview(cluster),
        }

    opportunities = []
    if card.post_ids:
        opportunities = list((await db.execute(
            select(Opportunity)
            .where(
                Opportunity.id.in_(card.post_ids),
                Opportunity.deleted_at == None,
                Opportunity.pipeline_version == PIPELINE_VERSION,
            )
            .order_by(Opportunity.source_score.desc().nullslast())
        )).scalars().all())

    return {
        **_serialize_pipeline_signal_payload(
            card,
            None,
            {
                **_build_signal_metrics(opportunities),
                "commentCount": sum(post.num_comments or 0 for post in opportunities),
                "averageUpvoteRatio": _avg([post.upvote_ratio for post in opportunities]),
            },
            opportunities,
        ),
        "analytics": None,
        "availability": {"clusterAnalytics": False, "brief": False},
        **serialize_signal_overview(None),
    }


@router.get("/{pipeline_id}/signal/evidence")
async def get_pipeline_signal_evidence(
    pipeline_id: str,
    q: str | None = Query(None),
    community: str | None = Query(None),
    sort: Literal["engagement", "newest", "comments", "similarity"] = Query("engagement"),
    page: int = Query(1, ge=1),
    pageSize: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Paginated, searchable evidence for a pipeline Signal workspace."""
    card = await _get_active_pipeline_card(pipeline_id, db, uid)
    cluster_id = _cluster_id_from_card(card)
    offset = (page - 1) * pageSize

    if cluster_id is not None:
        conditions = [
            ClusterMember.cluster_id == cluster_id,
            ClusterMember.pipeline_version == PIPELINE_VERSION,
            SourcePost.is_deleted.isnot(True),
            SourcePost.is_removed.isnot(True),
        ]
        if q:
            conditions.append(or_(SourcePost.title.ilike(f"%{q}%"), SourcePost.body.ilike(f"%{q}%")))
        if community:
            conditions.append(SourcePost.source_community_id == community)

        order = {
            "newest": func.coalesce(SourcePost.posted_at, SourcePost.scraped_at).desc().nullslast(),
            "comments": SourcePost.num_comments.desc().nullslast(),
            "similarity": ClusterMember.similarity_score.desc().nullslast(),
        }.get(sort, SourcePost.score.desc().nullslast())

        base = (
            select(ClusterMember, SourcePost)
            .join(SourcePost, ClusterMember.source_post_id == SourcePost.id)
            .where(*conditions)
        )
        total = await db.scalar(
            select(func.count())
            .select_from(ClusterMember)
            .join(SourcePost, ClusterMember.source_post_id == SourcePost.id)
            .where(*conditions)
        )
        rows = list((await db.execute(base.order_by(order).offset(offset).limit(pageSize))).all())
        communities = [
            value for (value,) in (await db.execute(
                select(SourcePost.source_community_id)
                .join(ClusterMember, ClusterMember.source_post_id == SourcePost.id)
                .where(
                    ClusterMember.cluster_id == cluster_id,
                    ClusterMember.pipeline_version == PIPELINE_VERSION,
                    SourcePost.is_deleted.isnot(True),
                    SourcePost.is_removed.isnot(True),
                    SourcePost.source_community_id.isnot(None),
                )
                .distinct()
                .order_by(SourcePost.source_community_id)
            )).all()
            if value
        ]
        return {
            "data": [_serialize_evidence_summary(member, post) for member, post in rows],
            "total": total or 0,
            "page": page,
            "pageSize": pageSize,
            "communities": communities,
        }

    conditions = [
        Opportunity.id.in_(card.post_ids or []),
        Opportunity.deleted_at == None,
        Opportunity.pipeline_version == PIPELINE_VERSION,
    ]
    if q:
        conditions.append(or_(Opportunity.title.ilike(f"%{q}%"), Opportunity.description.ilike(f"%{q}%")))
    if community:
        conditions.append(Opportunity.source == community)
    order = {
        "newest": Opportunity.created_at.desc().nullslast(),
        "comments": Opportunity.num_comments.desc().nullslast(),
    }.get(sort, Opportunity.source_score.desc().nullslast())
    total = await db.scalar(select(func.count()).select_from(Opportunity).where(*conditions))
    posts = list((await db.execute(
        select(Opportunity).where(*conditions).order_by(order).offset(offset).limit(pageSize)
    )).scalars().all())
    communities = [
        value for (value,) in (await db.execute(
            select(Opportunity.source)
            .where(
                Opportunity.id.in_(card.post_ids or []),
                Opportunity.deleted_at == None,
                Opportunity.pipeline_version == PIPELINE_VERSION,
                Opportunity.source.isnot(None),
            )
            .distinct()
            .order_by(Opportunity.source)
        )).all()
        if value
    ]
    return {
        "data": [_serialize_evidence_summary(None, post) for post in posts],
        "total": total or 0,
        "page": page,
        "pageSize": pageSize,
        "communities": communities,
    }


@router.get("/{pipeline_id}/signal/evidence/{post_id}")
async def get_pipeline_signal_evidence_detail(
    pipeline_id: str,
    post_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Full evidence post after verifying it belongs to the pipeline workspace."""
    card = await _get_active_pipeline_card(pipeline_id, db, uid)
    cluster_id = _cluster_id_from_card(card)
    if cluster_id is not None:
        rows = (await db.execute(
            select(ClusterMember, SourcePost)
            .join(SourcePost, ClusterMember.source_post_id == SourcePost.id)
            .where(
                ClusterMember.cluster_id == cluster_id,
                ClusterMember.pipeline_version == PIPELINE_VERSION,
                SourcePost.id == post_id,
                SourcePost.is_deleted.isnot(True),
                SourcePost.is_removed.isnot(True),
            )
        )).all()
        if rows:
            return _serialize_evidence_detail(rows[0][0], rows[0][1])
    elif post_id in (card.post_ids or []):
        post = (await db.execute(
            select(Opportunity).where(
                Opportunity.id == post_id,
                Opportunity.deleted_at == None,
                Opportunity.pipeline_version == PIPELINE_VERSION,
            )
        )).scalar_one_or_none()
        if post:
            return _serialize_evidence_detail(None, post)
    raise HTTPException(status_code=404, detail="Evidence post not found")


@router.get("/{pipeline_id}/signal/brief")
async def get_pipeline_signal_brief(
    pipeline_id: str,
    refresh: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Discovery brief scoped through an active user pipeline card."""
    card = await _get_active_pipeline_card(pipeline_id, db, uid)
    cluster_id = _cluster_id_from_card(card)
    if cluster_id is None:
        return {"available": False, "reason": "Cluster analytics are unavailable for this pipeline card."}
    return await get_discovery_brief(cluster_id=cluster_id, refresh=refresh, db=db, uid=uid)


@router.get("/{pipeline_id}/export")
async def export_pipeline_signal(
    pipeline_id: str,
    export_format: Literal["csv", "json"] = Query("csv", alias="format"),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Export the signal dataset backing the Signal workspace."""
    card, cluster_payload, metrics, posts = await _load_pipeline_signal_data(pipeline_id, db, uid)

    if export_format == "json":
        return _serialize_pipeline_signal_payload(card, cluster_payload, metrics, posts)

    csv_body = _build_signal_csv(card, cluster_payload, posts)
    return Response(
        content=csv_body,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="problemfinder-{pipeline_id}-signals.csv"',
        },
    )


@router.post("")
async def create_pipeline(
    body: CreatePipelineBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    if not body.post_ids:
        raise HTTPException(status_code=400, detail="post_ids must not be empty")

    # Duplicate check: any active card already contains one of these post_ids
    existing_result = await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.launched_at == None,
            Pipeline.removed_at == None,
        )
    )
    active_cards = existing_result.scalars().all()

    # Free plan: cap active pipeline cards (403 with limit_reached on overflow).
    plan = await get_plan(uid, db)
    cap = limit_for(plan, "max_pipeline_cards")
    if cap is not None and len(active_cards) >= cap:
        raise LimitReached("pipeline", plan, status_code=403)

    new_ids_set = set(body.post_ids)
    for card in active_cards:
        if new_ids_set.intersection(card.post_ids or []):
            raise HTTPException(
                status_code=409,
                detail={"error": "DUPLICATE_POST_IDS", "existingClusterId": card.id},
            )

    card = Pipeline(
        user_id=uid,
        name=body.name,
        post_ids=body.post_ids,
        source_cluster_id=body.source_cluster_id,
    )
    db.add(card)
    await db.flush()

    for post_id in body.post_ids:
        await _update_opportunity_counts(post_id, "add", db)

    await db.commit()
    await db.refresh(card)

    background_tasks.add_task(_compute_and_write_metrics, str(card.id), list(body.post_ids))

    posts_map = await _fetch_posts_for_cards([card], db)
    return {"data": _serialize(card, posts=posts_map.get(str(card.id), []))}


@router.patch("/{pipeline_id}")
async def patch_pipeline(
    pipeline_id: str,
    body: PatchPipelineBody,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    updates = body.model_dump(exclude_none=True)

    if "stage" in updates and updates["stage"] not in VALID_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid stage '{updates['stage']}'. Must be one of: {', '.join(PIPELINE_STAGES)}",
        )

    post_ids_changed = "post_ids" in updates

    for field, value in updates.items():
        setattr(card, field, value)
    if "outcome" in updates or "mrr" in updates:
        card.outcome_noted_at = datetime.now(timezone.utc)
    card.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(card)

    if post_ids_changed:
        background_tasks.add_task(_compute_and_write_metrics, str(card.id), list(card.post_ids or []))

    posts_map = await _fetch_posts_for_cards([card], db)
    return {"data": _serialize(card, posts=posts_map.get(str(card.id), []))}


@router.post("/{pipeline_id}/launch")
async def launch_pipeline(
    pipeline_id: str,
    body: LaunchBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")
    if card.launched_at:
        raise HTTPException(status_code=409, detail="Already launched")

    card.launched_at = datetime.now(timezone.utc)
    card.name = body.product_name
    card.updated_at = datetime.now(timezone.utc)

    for post_id in card.post_ids or []:
        await _update_opportunity_counts(post_id, "launch", db)

    await db.commit()
    await db.refresh(card)
    return {"data": _serialize(card)}


@router.delete("/{pipeline_id}")
async def delete_pipeline(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    card.removed_at = datetime.now(timezone.utc)
    card.updated_at = datetime.now(timezone.utc)

    for post_id in card.post_ids or []:
        await _update_opportunity_counts(post_id, "remove", db)

    await db.commit()
    return {"success": True}
