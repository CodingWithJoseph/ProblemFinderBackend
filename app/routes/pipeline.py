import csv
import io
from datetime import datetime, timezone
from typing import Literal, Optional
import math
import random
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db import get_db, AsyncSessionLocal
from app.auth import get_uid
from app.models import Pipeline, Opportunity, Cluster
from app.limits import get_plan, limit_for, LimitReached

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

INTENSITY_MAP = {"low": 1.0, "medium": 2.0, "high": 3.0}
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
    "domain",
    "author",
    "signal_score",
    "wtp_signal",
    "workaround_boolean",
    "intensity",
    "sentiment",
    "intensity_score",
    "sentiment_score",
    "wtp_confidence",
    "workaround_confidence",
    "pain_score",
    "urgency_score",
    "commercial_intent_score",
    "solution_seeking_score",
    "signal_strength",
    "signal_reason",
    "post_intent",
    "industry_category",
    "num_comments",
    "created_at",
)


def _format_cluster_metrics(raw: dict | None) -> dict | None:
    if not raw:
        return None
    return {
        "avgSignalScore": raw.get("avg_signal_score"),
        "wtpRate": raw.get("wtp_rate"),
        "workaroundRate": raw.get("workaround_rate"),
        "avgIntensity": raw.get("avg_intensity"),
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
        "domain": o.domain,
        "author": o.author,
        "signalScore": o.signal_score,
        "wtpSignal": o.wtp_signal,
        "workaroundBoolean": o.workaround_boolean,
        "intensity": o.intensity,
        "sentiment": o.sentiment,
        "intensityScore": o.intensity_score,
        "sentimentScore": o.sentiment_score,
        "wtpConfidence": o.wtp_confidence,
        "workaroundConfidence": o.workaround_confidence,
        "painScore": o.pain_score,
        "urgencyScore": o.urgency_score,
        "commercialIntentScore": o.commercial_intent_score,
        "solutionSeekingScore": o.solution_seeking_score,
        "signalStrength": o.signal_strength,
        "signalReason": o.signal_reason,
        "postIntent": o.post_intent,
        "industryCategory": o.industry_category,
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
        "signalScore": cluster.signal_score,
        "trending": cluster.trending,
        "growthRateWow": cluster.growth_rate_wow,
        "growthRateMom": cluster.growth_rate_mom,
        "postVelocity": cluster.post_velocity,
    }


def _build_signal_metrics(posts: list[Opportunity]) -> dict:
    count = len(posts)
    return {
        "postCount": count,
        "wtpRate": round(sum(1 for p in posts if p.wtp_signal) / count, 4) if count else None,
        "workaroundRate": round(sum(1 for p in posts if p.workaround_boolean) / count, 4) if count else None,
        "avgIntensityScore": _avg([p.intensity_score for p in posts]),
        "avgSentimentScore": _avg([p.sentiment_score for p in posts]),
        "avgSignalScore": _avg([p.signal_score for p in posts]),
        "avgWtpConfidence": _avg([p.wtp_confidence for p in posts]),
        "avgWorkaroundConfidence": _avg([p.workaround_confidence for p in posts]),
        "avgPainScore": _avg([p.pain_score for p in posts]),
        "avgUrgencyScore": _avg([p.urgency_score for p in posts]),
        "avgCommercialIntentScore": _avg([p.commercial_intent_score for p in posts]),
        "avgSolutionSeekingScore": _avg([p.solution_seeking_score for p in posts]),
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
        cluster_result = await db.execute(select(Cluster).where(Cluster.id == cluster_int_id))
        cluster = cluster_result.scalar_one_or_none()

    # Full cluster view: ALL posts in the cluster; fall back to the card's post_ids.
    if cluster_int_id is not None:
        posts_result = await db.execute(
            select(Opportunity)
            .where(Opportunity.cluster_id == cluster_int_id, Opportunity.deleted_at == None)
            .order_by(Opportunity.signal_score.desc().nullslast())
        )
        posts = list(posts_result.scalars().all())
    elif card.post_ids:
        posts_result = await db.execute(
            select(Opportunity)
            .where(Opportunity.id.in_(card.post_ids), Opportunity.deleted_at == None)
            .order_by(Opportunity.signal_score.desc().nullslast())
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
            "domain": post.domain,
            "author": post.author,
            "signal_score": post.signal_score,
            "wtp_signal": post.wtp_signal,
            "workaround_boolean": post.workaround_boolean,
            "intensity": post.intensity,
            "sentiment": post.sentiment,
            "intensity_score": post.intensity_score,
            "sentiment_score": post.sentiment_score,
            "wtp_confidence": post.wtp_confidence,
            "workaround_confidence": post.workaround_confidence,
            "pain_score": post.pain_score,
            "urgency_score": post.urgency_score,
            "commercial_intent_score": post.commercial_intent_score,
            "solution_seeking_score": post.solution_seeking_score,
            "signal_strength": post.signal_strength,
            "signal_reason": post.signal_reason,
            "post_intent": post.post_intent,
            "industry_category": post.industry_category,
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
                    Opportunity.signal_score,
                    Opportunity.wtp_signal,
                    Opportunity.workaround_boolean,
                    Opportunity.intensity,
                ).where(Opportunity.id.in_(post_ids))
            )
            rows = result.all()

            if not rows:
                return

            count = len(rows)
            scores = [r.signal_score for r in rows if r.signal_score is not None]
            wtp_count = sum(1 for r in rows if r.wtp_signal)
            workaround_count = sum(1 for r in rows if r.workaround_boolean)
            intensities = [INTENSITY_MAP[r.intensity] for r in rows if r.intensity in INTENSITY_MAP]

            avg_signal_score = round(sum(scores) / len(scores), 2) if scores else None
            wtp_rate = round(wtp_count / count, 4)
            workaround_rate = round(workaround_count / count, 4)
            avg_intensity = round(sum(intensities) / len(intensities), 4) if intensities else None

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
                        "WHERE id::text = ANY(:ids) AND embedding IS NOT NULL"
                    ),
                    {"ids": sample},
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
                "avg_signal_score": avg_signal_score,
                "wtp_rate": wtp_rate,
                "workaround_rate": workaround_rate,
                "avg_intensity": avg_intensity,
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
            pass  # Background task - don't surface errors to the user


async def _fetch_posts_for_cards(cards: list[Pipeline], db: AsyncSession) -> dict[str, list[dict]]:
    """Batch-fetch {id, title, signalScore} for all post_ids across the given pipeline cards."""
    all_ids = list({pid for c in cards for pid in (c.post_ids or [])})
    if not all_ids:
        return {str(c.id): [] for c in cards}

    result = await db.execute(
        select(Opportunity.id, Opportunity.title, Opportunity.signal_score)
        .where(Opportunity.id.in_(all_ids))
    )
    post_map: dict[str, dict] = {
        str(row.id): {"id": str(row.id), "title": row.title, "signalScore": row.signal_score}
        for row in result
    }

    return {
        str(c.id): [post_map[pid] for pid in (c.post_ids or []) if pid in post_map]
        for c in cards
    }


async def _update_opportunity_counts(opportunity_id: str, action: str, db: AsyncSession) -> None:
    result = await db.execute(select(Opportunity).where(Opportunity.id == opportunity_id))
    opp = result.scalar_one_or_none()
    if not opp:
        return
    pipeline_count = opp.pipeline_count or 0
    launch_count = opp.launch_count or 0
    if action == "add":
        opp.pipeline_count = pipeline_count + 1
    elif action == "remove":
        opp.pipeline_count = max(0, pipeline_count - 1)
    elif action == "launch":
        opp.pipeline_count = max(0, pipeline_count - 1)
        opp.launch_count = launch_count + 1


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


@router.get("/{pipeline_id}/signal")
async def get_pipeline_signal(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Cluster metrics + all cluster posts with full signal breakdown.

    The pipeline is the anchor (notes live here). Posts are the full set in the
    pipeline's source_cluster_id; falls back to the card's post_ids if there is
    no system cluster behind it.
    """
    card, cluster_payload, metrics, posts = await _load_pipeline_signal_data(pipeline_id, db, uid)
    return _serialize_pipeline_signal_payload(card, cluster_payload, metrics, posts)


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
