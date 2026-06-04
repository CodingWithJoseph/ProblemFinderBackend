import uuid
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, cast, Date, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from app.db import get_db
from app.auth import get_uid
from app.config import PIPELINE_VERSION
from app.openai_client import openai_client
from app.models import Opportunity, SearchUsage, Subscription, PlanEnum, Cluster, ClusterSnapshot, Problem, Pipeline, UserFlag
from app.limits import enforce_search_limit
from app.anthropic_client import generate_next_step

router = APIRouter(prefix="/opportunities", tags=["opportunities"])

COUNT_CAP = 10_000
MAX_PAGE = 500

# Columns actually mapped on the Opportunity model. Used to rebuild ORM objects
# from raw ``SELECT *`` rows (hybrid_search / similar-posts) while ignoring stale
# DB columns the simplified pipeline no longer writes.
_OPPORTUNITY_COLUMNS = {c.name for c in Opportunity.__table__.columns}


def _row_to_opportunity(row) -> Opportunity:
    return Opportunity(**{k: v for k, v in dict(row).items() if k in _OPPORTUNITY_COLUMNS})


def _serialize(o: Opportunity) -> dict:
    return {
        "id": o.id,
        "title": o.title,
        "description": o.description,
        "source": o.source,
        "sourceUrl": o.source_url,
        "opportunityType": o.opportunity_type,
        "sourceScore": o.source_score,
        "trending": o.trending,
        "clusterId": o.cluster_id,
        "numComments": o.num_comments,
        "upvoteRatio": o.upvote_ratio,
        "feed": o.feed,
        "author": o.author,
        "scrapedAt": o.scraped_at,
        "createdAt": o.created_at,
        "pipelineCount": o.pipeline_count,
        "launchCount": o.launch_count,
        "recommendedNextStep": o.recommended_next_step,
        "topComments": o.top_comments,
    }


def _serialize_cluster_inline(c: Cluster, post_count: int | None) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "postCount": post_count,
        "trending": c.trending,
        "growthRateWow": c.growth_rate_wow,
        "growthRateMom": c.growth_rate_mom,
        "postVelocity": c.post_velocity,
    }


def _serialize_flag(flag: UserFlag) -> dict:
    return {
        "id": flag.id,
        "opportunityId": flag.opportunity_id,
        "pipelineId": flag.pipeline_id,
        "reason": flag.reason,
        "note": flag.note,
        "hidden": flag.hidden,
        "createdAt": flag.created_at.isoformat() if flag.created_at else None,
        "updatedAt": flag.updated_at.isoformat() if flag.updated_at else None,
    }


async def _build_cluster_map(opportunities: list[Opportunity], db: AsyncSession) -> dict:
    """Map cluster_id -> serialized cluster (with latest snapshot post count)."""
    cluster_ids = list({o.cluster_id for o in opportunities if o.cluster_id is not None})
    if not cluster_ids:
        return {}

    clusters_result = await db.execute(
        select(Cluster).where(
            Cluster.id.in_(cluster_ids),
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
    )
    clusters = clusters_result.scalars().all()

    snapshots_result = await db.execute(
        select(ClusterSnapshot)
        .where(
            ClusterSnapshot.cluster_id.in_(cluster_ids),
        )
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
    opportunity_type: str | None = Query(None),
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

    query = select(Opportunity).where(
        Opportunity.deleted_at == None,
        Opportunity.pipeline_version == PIPELINE_VERSION,
    )

    if opportunity_type:
        query = query.where(Opportunity.opportunity_type == opportunity_type)
    if has_cluster is True:
        query = query.where(Opportunity.cluster_id != None)
    elif has_cluster is False:
        query = query.where(Opportunity.cluster_id == None)
    if cluster_id is not None:
        query = query.where(Opportunity.cluster_id == cluster_id)

    if sort == "trending":
        query = query.where(Opportunity.trending == True)
        query = query.order_by(Opportunity.source_score.desc().nullslast())
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
            Opportunity.pipeline_version == PIPELINE_VERSION,
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
    """Top opportunities by source score (for the dashboard right column)."""
    result = await db.execute(
        select(Opportunity)
        .where(
            Opportunity.deleted_at == None,
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
        .order_by(Opportunity.source_score.desc().nullslast())
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
    # hybrid_search RETURNS SETOF opportunities (SELECT *), which can include
    # stale scoring columns no longer mapped on the model. Whitelist to mapped
    # columns so the reconstruction stays null-safe after JOB 3 removals.
    opportunities = [_row_to_opportunity(row) for row in rows]

    cluster_map = await _build_cluster_map(opportunities, db)

    # Search clusters by name similarity
    matching_clusters_result = await db.execute(
        select(Cluster)
        .where(
            Cluster.name.ilike(f"%{q}%"),
            Cluster.pipeline_version == PIPELINE_VERSION,
        )
        .limit(5)
    )
    matching_clusters = matching_clusters_result.scalars().all()

    cluster_search_ids = [c.id for c in matching_clusters]
    cluster_snapshot_map: dict[int, int | None] = {}
    if cluster_search_ids:
        cluster_snapshots_result = await db.execute(
            select(ClusterSnapshot)
            .where(
                ClusterSnapshot.cluster_id.in_(cluster_search_ids),
            )
            .distinct(ClusterSnapshot.cluster_id)
            .order_by(ClusterSnapshot.cluster_id, ClusterSnapshot.date.desc())
        )
        for s in cluster_snapshots_result.scalars().all():
            cluster_snapshot_map[s.cluster_id] = s.post_count

    clusters_response = [
        {
            "id": c.id,
            "name": c.name,
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


class FlagOpportunityRequest(BaseModel):
    reason: Literal["irrelevant", "duplicate", "low_quality", "spam", "other"]
    note: str | None = None
    pipeline_id: str | None = None
    hidden: bool = True


@router.post("/{opportunity_id}/flag")
async def flag_opportunity(
    opportunity_id: str,
    body: FlagOpportunityRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    opportunity = (await db.execute(
        select(Opportunity).where(
            Opportunity.id == opportunity_id,
            Opportunity.deleted_at == None,
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
    )).scalar_one_or_none()
    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    existing = (await db.execute(
        select(UserFlag).where(
            UserFlag.user_id == uid,
            UserFlag.opportunity_id == opportunity_id,
        )
    )).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if existing:
        existing.reason = body.reason
        existing.note = body.note
        existing.pipeline_id = body.pipeline_id
        existing.hidden = body.hidden
        existing.updated_at = now
        flag = existing
    else:
        flag = UserFlag(
            user_id=uid,
            opportunity_id=opportunity_id,
            pipeline_id=body.pipeline_id,
            reason=body.reason,
            note=body.note,
            hidden=body.hidden,
            created_at=now,
            updated_at=now,
        )
        db.add(flag)

    await db.commit()
    await db.refresh(flag)
    return {"data": _serialize_flag(flag)}


@router.delete("/{opportunity_id}/flag")
async def clear_opportunity_flag(
    opportunity_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    flag = (await db.execute(
        select(UserFlag).where(
            UserFlag.user_id == uid,
            UserFlag.opportunity_id == opportunity_id,
        )
    )).scalar_one_or_none()
    if not flag:
        raise HTTPException(status_code=404, detail="Flag not found")

    await db.delete(flag)
    await db.commit()
    return {"success": True}


@router.post("/{opportunity_id}/pipeline-action")
async def pipeline_action(
    opportunity_id: str,
    body: PipelineActionRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Opportunity).where(
            Opportunity.id == opportunity_id,
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
    )
    opportunity = result.scalar_one_or_none()

    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    pipeline_count = opportunity.pipeline_count or 0
    launch_count = opportunity.launch_count or 0

    # Mutate counters atomically in SQL (column = column +/- 1, floored at 0) so
    # concurrent actions can't lose updates. The response echoes the expected
    # post-action values computed from the row we just read.
    pcount = func.coalesce(Opportunity.pipeline_count, 0)
    if body.action == "add":
        values = {"pipeline_count": pcount + 1}
        new_pipeline_count, new_launch_count = pipeline_count + 1, launch_count
    elif body.action == "remove":
        values = {"pipeline_count": func.greatest(0, pcount - 1)}
        new_pipeline_count, new_launch_count = max(0, pipeline_count - 1), launch_count
    elif body.action == "launch":
        values = {
            "pipeline_count": func.greatest(0, pcount - 1),
            "launch_count": func.coalesce(Opportunity.launch_count, 0) + 1,
        }
        new_pipeline_count, new_launch_count = max(0, pipeline_count - 1), launch_count + 1
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

    await db.execute(
        update(Opportunity).where(Opportunity.id == opportunity_id).values(**values)
    )
    await db.commit()

    return {
        "success": True,
        "pipelineCount": new_pipeline_count,
        "launchCount": new_launch_count,
    }


def _build_next_step_context(opp: Opportunity, problems: list[Problem], cluster: Cluster | None) -> str:
    lines = [f"OPPORTUNITY: {opp.title}"]
    if opp.description:
        lines.append(f"Description: {opp.description[:600]}")

    if cluster is not None:
        lines.append("")
        lines.append(f"CLUSTER: {cluster.name or 'unnamed'}")
        cluster_bits = []
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
        select(Opportunity).where(
            Opportunity.id == opportunity_id,
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
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
            select(Cluster).where(
                Cluster.id == opp.cluster_id,
                Cluster.pipeline_version == PIPELINE_VERSION,
            )
        )).scalar_one_or_none()

    context = _build_next_step_context(opp, problems, cluster)
    recommendation = await generate_next_step(context)
    if recommendation:
        opp.recommended_next_step = recommendation
        await db.commit()

    return {"data": {"recommendedNextStep": opp.recommended_next_step}}
