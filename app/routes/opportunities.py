import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, cast, Date, text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from app.db import get_db
from app.auth import get_uid
from app.openai_client import openai_client
from app.models import Opportunity, SearchUsage, Subscription, PlanEnum, Cluster, ClusterSnapshot, Problem, Pipeline
from app.limits import enforce_search_limit
from app.anthropic_client import generate_next_step

router = APIRouter(prefix="/opportunities", tags=["opportunities"])

COUNT_CAP = 10_000
MAX_PAGE = 500


def _serialize(o: Opportunity) -> dict:
    return {
        "id": o.id,
        "title": o.title,
        "description": o.description,
        "source": o.source,
        "sourceUrl": o.source_url,
        "domain": o.domain,
        "type": o.type,
        "sourceScore": o.source_score,
        "signalScore": o.signal_score,
        "intensityScore": o.intensity_score,
        "sentimentScore": o.sentiment_score,
        "wtpConfidence": o.wtp_confidence,
        "workaroundConfidence": o.workaround_confidence,
        "upvotes": o.upvotes,
        "trending": o.trending,
        "wtpSignal": o.wtp_signal,
        "workaroundBoolean": o.workaround_boolean,
        "intensity": o.intensity,
        "sentiment": o.sentiment,
        "clusterId": o.cluster_id,
        "numComments": o.num_comments,
        "upvoteRatio": o.upvote_ratio,
        "feed": o.feed,
        "author": o.author,
        "scrapedAt": o.scraped_at,
        "createdAt": o.created_at,
        "pipelineCount": o.pipeline_count,
        "launchCount": o.launch_count,
        "signalStrength": o.signal_strength,
        "signalReason": o.signal_reason,
        "industryCategory": o.industry_category,
        "painScore": o.pain_score,
        "urgencyScore": o.urgency_score,
        "commercialIntentScore": o.commercial_intent_score,
        "solutionSeekingScore": o.solution_seeking_score,
        "revenuePotentialScore": o.revenue_potential_score,
        "confidenceScore": o.confidence_score,
        "primaryProblem": o.primary_problem,
        "targetAudience": o.target_audience,
        "buyerPersona": o.buyer_persona,
        "businessModel": o.business_model,
        "monetizationPath": o.monetization_path,
        "estimatedWillingnessToPay": o.estimated_willingness_to_pay,
        "competitionLevel": o.competition_level,
        "marketSaturation": o.market_saturation,
        "recommendedNextStep": o.recommended_next_step,
        "topComments": o.top_comments,
        "needsReview": o.needs_review,
    }


def _serialize_cluster_inline(c: Cluster, post_count: int | None) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "postCount": post_count,
        "signalScore": c.signal_score,
        "trending": c.trending,
        "growthRateWow": c.growth_rate_wow,
        "growthRateMom": c.growth_rate_mom,
        "postVelocity": c.post_velocity,
    }


async def _build_cluster_map(opportunities: list[Opportunity], db: AsyncSession) -> dict:
    """Map cluster_id -> serialized cluster (with latest snapshot post count)."""
    cluster_ids = list({o.cluster_id for o in opportunities if o.cluster_id is not None})
    if not cluster_ids:
        return {}

    clusters_result = await db.execute(select(Cluster).where(Cluster.id.in_(cluster_ids)))
    clusters = clusters_result.scalars().all()

    snapshots_result = await db.execute(
        select(ClusterSnapshot)
        .where(ClusterSnapshot.cluster_id.in_(cluster_ids))
        .distinct(ClusterSnapshot.cluster_id)
        .order_by(ClusterSnapshot.cluster_id, ClusterSnapshot.date.desc())
    )
    snapshot_map = {s.cluster_id: s.post_count for s in snapshots_result.scalars().all()}

    return {c.id: _serialize_cluster_inline(c, snapshot_map.get(c.id)) for c in clusters}


async def _get_plan(uid: str, db: AsyncSession) -> PlanEnum:
    sub_result = await db.execute(
        select(Subscription).where(Subscription.uid == uid)
    )
    sub = sub_result.scalar_one_or_none()
    return sub.plan if sub else PlanEnum.free


@router.get("")
async def get_opportunities(
    page: int = Query(1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),
    type: str | None = Query(None),
    min_score: float | None = Query(None),
    domain: str | None = Query(None),
    intensity: str | None = Query(None),
    sentiment: str | None = Query(None),
    wtp_signal: bool | None = Query(None),
    workaround_boolean: bool | None = Query(None),
    has_cluster: bool | None = Query(None),
    cluster_id: int | None = Query(None),
    sort: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    plan = await _get_plan(uid, db)

    if plan == PlanEnum.free:
        page = min(page, 1)
        pageSize = min(pageSize, 50)

    page = min(page, MAX_PAGE)
    offset = (page - 1) * pageSize

    query = select(Opportunity).where(Opportunity.deleted_at == None)

    if type:
        query = query.where(Opportunity.type == type)
    if domain:
        query = query.where(Opportunity.domain == domain.lower())
    if intensity:
        query = query.where(Opportunity.intensity == intensity.lower())
    if sentiment:
        query = query.where(Opportunity.sentiment == sentiment.lower())
    if wtp_signal is not None:
        query = query.where(Opportunity.wtp_signal == wtp_signal)
    if workaround_boolean is not None:
        query = query.where(Opportunity.workaround_boolean == workaround_boolean)
    if min_score is not None:
        query = query.where(Opportunity.signal_score >= min_score)
    if has_cluster is True:
        query = query.where(Opportunity.cluster_id != None)
    elif has_cluster is False:
        query = query.where(Opportunity.cluster_id == None)
    if cluster_id is not None:
        query = query.where(Opportunity.cluster_id == cluster_id)

    if sort == "trending":
        query = query.where(Opportunity.trending == True)
        query = query.order_by(Opportunity.signal_score.desc().nullslast())
    elif sort == "signal_score":
        query = query.order_by(Opportunity.signal_score.desc().nullslast())
    elif sort == "source_score":
        query = query.order_by(Opportunity.source_score.desc().nullslast())
    else:
        query = query.order_by(Opportunity.created_at.desc().nullslast())

    count_query = select(func.count()).select_from(
        query.limit(COUNT_CAP).subquery()
    )
    total = await db.scalar(count_query)

    query = query.offset(offset).limit(pageSize)
    result = await db.execute(query)
    opportunities = result.scalars().all()

    cluster_map = await _build_cluster_map(list(opportunities), db)

    def _serialize_with_cluster(o: Opportunity) -> dict:
        return {
            **_serialize(o),
            "cluster": cluster_map.get(o.cluster_id) if o.cluster_id else None,
        }

    return {
        "data": [_serialize_with_cluster(o) for o in opportunities],
        "page": page,
        "pageSize": pageSize,
        "total": total,
        "capped": total == COUNT_CAP,
    }


@router.get("/batch")
async def get_opportunities_by_ids(
    ids: str = Query(..., description="Comma-separated opportunity IDs"),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    id_list = [i.strip() for i in ids.split(",") if i.strip()]

    if not id_list:
        return {"data": []}

    uuid_list = [uuid.UUID(i) for i in id_list]

    result = await db.execute(
        select(Opportunity).where(
            Opportunity.id.in_(uuid_list),
            Opportunity.deleted_at == None,
        )
    )
    opportunities = result.scalars().all()

    return {"data": [_serialize(o) for o in opportunities]}


@router.get("/top")
async def get_top_opportunities(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Top opportunities by signal score (for the dashboard right column)."""
    result = await db.execute(
        select(Opportunity)
        .where(Opportunity.deleted_at == None)
        .order_by(Opportunity.signal_score.desc().nullslast())
        .limit(limit)
    )
    opportunities = list(result.scalars().all())
    cluster_map = await _build_cluster_map(opportunities, db)

    return {
        "data": [
            {**_serialize(o), "cluster": cluster_map.get(o.cluster_id) if o.cluster_id else None}
            for o in opportunities
        ]
    }


@router.get("/search")
async def search_opportunities(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    plan = await _get_plan(uid, db)

    # Free: 3 searches per rolling 24h (429 with limit_reached on overflow).
    await enforce_search_limit(uid, plan, db)

    response = await openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=q,
    )
    query_vector = response.data[0].embedding
    vector_str = f"[{','.join(str(x) for x in query_vector)}]"

    page = min(page, MAX_PAGE)
    offset = (page - 1) * pageSize

    hybrid_query = text(f"""
        SELECT * FROM hybrid_search(
            :query_text,
            '{vector_str}'::vector,
            :match_count
        )
        LIMIT :limit OFFSET :offset
    """)

    count_query = text(f"""
        SELECT COUNT(*) FROM hybrid_search(
            :query_text,
            '{vector_str}'::vector,
            :match_count
        )
    """)

    total = await db.scalar(
        count_query,
        {"query_text": q, "match_count": 200}
    )

    result = await db.execute(
        hybrid_query,
        {
            "query_text": q,
            "match_count": 200,
            "limit": pageSize,
            "offset": offset,
        }
    )

    rows = result.mappings().all()
    EXCLUDE_COLS = {"embedding", "search_vector"}
    opportunities = [Opportunity(**{k: v for k, v in dict(row).items() if k not in EXCLUDE_COLS}) for row in rows]

    cluster_map = await _build_cluster_map(opportunities, db)

    # Search clusters by name similarity
    matching_clusters_result = await db.execute(
        select(Cluster)
        .where(Cluster.name.ilike(f"%{q}%"))
        .limit(5)
    )
    matching_clusters = matching_clusters_result.scalars().all()

    cluster_search_ids = [c.id for c in matching_clusters]
    cluster_snapshot_map: dict[int, int | None] = {}
    if cluster_search_ids:
        cluster_snapshots_result = await db.execute(
            select(ClusterSnapshot)
            .where(ClusterSnapshot.cluster_id.in_(cluster_search_ids))
            .distinct(ClusterSnapshot.cluster_id)
            .order_by(ClusterSnapshot.cluster_id, ClusterSnapshot.date.desc())
        )
        for s in cluster_snapshots_result.scalars().all():
            cluster_snapshot_map[s.cluster_id] = s.post_count

    clusters_response = [
        {
            "id": c.id,
            "name": c.name,
            "signalScore": c.signal_score,
            "postCount": cluster_snapshot_map.get(c.id),
            "trending": c.trending,
            "growthRateWow": c.growth_rate_wow,
            "growthRateMom": c.growth_rate_mom,
            "postVelocity": c.post_velocity,
        }
        for c in matching_clusters
    ]

    db.add(SearchUsage(
        uid=uid,
        query=q,
        result_count=total or 0,
        searched_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))

    def _serialize_with_cluster(o: Opportunity) -> dict:
        return {
            **_serialize(o),
            "cluster": cluster_map.get(o.cluster_id) if o.cluster_id else None,
        }

    return {
        "data": [_serialize_with_cluster(o) for o in opportunities],
        "page": page,
        "pageSize": pageSize,
        "total": total,
        "clusters": clusters_response,
    }


class PipelineActionRequest(BaseModel):
    action: str  # "add" | "remove" | "launch"


@router.post("/{opportunity_id}/pipeline-action")
async def pipeline_action(
    opportunity_id: str,
    body: PipelineActionRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Opportunity).where(Opportunity.id == opportunity_id)
    )
    opportunity = result.scalar_one_or_none()

    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    pipeline_count = opportunity.pipeline_count or 0
    launch_count = opportunity.launch_count or 0

    if body.action == "add":
        opportunity.pipeline_count = pipeline_count + 1
    elif body.action == "remove":
        opportunity.pipeline_count = max(0, pipeline_count - 1)
    elif body.action == "launch":
        opportunity.pipeline_count = max(0, pipeline_count - 1)
        opportunity.launch_count = launch_count + 1
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

    await db.commit()

    return {
        "success": True,
        "pipelineCount": opportunity.pipeline_count,
        "launchCount": opportunity.launch_count,
    }


def _build_next_step_context(opp: Opportunity, problems: list[Problem], cluster: Cluster | None) -> str:
    lines = [f"OPPORTUNITY: {opp.title}"]
    if opp.description:
        lines.append(f"Description: {opp.description[:600]}")
    if opp.signal_strength:
        reason = f" ({opp.signal_reason})" if opp.signal_reason else ""
        lines.append(f"Signal strength: {opp.signal_strength}{reason}")
    if opp.industry_category:
        lines.append(f"Industry: {opp.industry_category}")
    if opp.primary_problem:
        lines.append(f"Primary problem: {opp.primary_problem}")
    if opp.target_audience:
        lines.append(f"Target audience: {opp.target_audience}")
    score_bits = [
        f"{label} {round(val, 2)}"
        for label, val in (
            ("pain", opp.pain_score),
            ("urgency", opp.urgency_score),
            ("commercial intent", opp.commercial_intent_score),
            ("solution-seeking", opp.solution_seeking_score),
        )
        if val is not None
    ]
    if score_bits:
        lines.append("Signal scores: " + ", ".join(score_bits))

    if cluster is not None:
        lines.append("")
        lines.append(f"CLUSTER: {cluster.name or 'unnamed'}")
        if cluster.description:
            lines.append(f"Cluster description: {cluster.description[:400]}")
        cluster_bits = []
        if cluster.signal_score is not None:
            cluster_bits.append(f"signal {round(cluster.signal_score)}")
        if cluster.growth_rate_wow is not None:
            cluster_bits.append(f"WoW growth {round(cluster.growth_rate_wow * 100)}%")
        if cluster.post_velocity is not None:
            cluster_bits.append(f"{round(cluster.post_velocity)} posts/week")
        if cluster_bits:
            lines.append("Cluster metrics: " + ", ".join(cluster_bits))

    lines.append("")
    if problems:
        lines.append("DEFINED PROBLEMS:")
        for p in problems[:15]:
            extras = []
            if p.pain_level:
                extras.append(f"pain {p.pain_level}")
            if p.urgency_level:
                extras.append(f"urgency {p.urgency_level}")
            if p.willingness_to_pay_signal:
                extras.append(f"WTP {p.willingness_to_pay_signal}")
            suffix = f" [{', '.join(extras)}]" if extras else ""
            lines.append(f"- {p.title}{suffix}")
    else:
        lines.append("DEFINED PROBLEMS: none yet.")

    lines.append("")
    lines.append("Recommend the single most valuable next step for this founder.")
    return "\n".join(lines)


@router.post("/{opportunity_id}/recommend-next-step")
async def recommend_next_step(
    opportunity_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Generate (via Claude) and persist a recommended next step for an opportunity,
    grounded in the user's defined problems, the opportunity's signal strength, and its
    cluster. Intended to be called once the user has defined problems for the pipeline.
    Idempotent — re-call to refresh."""
    opp = (await db.execute(
        select(Opportunity).where(Opportunity.id == opportunity_id)
    )).scalar_one_or_none()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    # The user's pipelines that contain this opportunity, and their problems.
    pipelines = (await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.post_ids.any(opportunity_id),
        )
    )).scalars().all()
    pipeline_ids = [p.id for p in pipelines]

    problems: list[Problem] = []
    if pipeline_ids:
        problems = list((await db.execute(
            select(Problem).where(Problem.pipeline_id.in_(pipeline_ids))
        )).scalars().all())

    cluster = None
    if opp.cluster_id is not None:
        cluster = (await db.execute(
            select(Cluster).where(Cluster.id == opp.cluster_id)
        )).scalar_one_or_none()

    context = _build_next_step_context(opp, problems, cluster)
    recommendation = await generate_next_step(context)
    if recommendation:
        opp.recommended_next_step = recommendation
        await db.commit()

    return {"data": {"recommendedNextStep": opp.recommended_next_step}}
