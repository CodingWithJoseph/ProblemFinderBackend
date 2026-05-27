from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, cast, Date, text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from openai import AsyncOpenAI
from app.db import get_db
from app.auth import get_uid
from app.models import Opportunity, SearchUsage, Subscription, PlanEnum, Cluster, ClusterSnapshot

router = APIRouter(prefix="/opportunities", tags=["opportunities"])
openai_client = AsyncOpenAI()


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
    }


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
    domain: str | None = Query(None),
    intensity: str | None = Query(None),
    sentiment: str | None = Query(None),
    wtp_signal: bool | None = Query(None),
    workaround_boolean: bool | None = Query(None),
    sort: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    plan = await _get_plan(uid, db)

    if plan == PlanEnum.free:
        page = min(page, 1)
        pageSize = min(pageSize, 50)

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

    if sort == "trending":
        query = query.where(Opportunity.trending == True)
    elif sort == "source_score":
        query = query.order_by(Opportunity.source_score.desc())
    else:
        query = query.order_by(Opportunity.created_at.desc())

    count_query = select(func.count()).select_from(query.subquery())
    total = await db.scalar(count_query)

    query = query.offset(offset).limit(pageSize)
    result = await db.execute(query)
    opportunities = result.scalars().all()

    return {
        "data": [_serialize(o) for o in opportunities],
        "page": page,
        "pageSize": pageSize,
        "total": total,
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

    result = await db.execute(
        select(Opportunity).where(
            Opportunity.id.in_(id_list),
            Opportunity.deleted_at == None,
        )
    )
    opportunities = result.scalars().all()

    return {"data": [_serialize(o) for o in opportunities]}


@router.get("/search")
async def search_opportunities(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    plan = await _get_plan(uid, db)

    if plan == PlanEnum.free:
        today = datetime.now(timezone.utc).date()
        count_result = await db.execute(
            select(func.count()).where(
                SearchUsage.uid == uid,
                cast(SearchUsage.searched_at, Date) == today,
            )
        )
        search_count = count_result.scalar() or 0
        if search_count >= 5:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "Search limit reached",
                    "code": "SEARCH_LIMIT_REACHED",
                    "remaining": 0,
                }
            )

    # Embed query using OpenAI text-embedding-3-small
    response = await openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=q,
    )
    query_vector = response.data[0].embedding
    vector_str = f"[{','.join(str(x) for x in query_vector)}]"

    offset = (page - 1) * pageSize

    # Hybrid search via Supabase RPC function
    hybrid_query = text("""
        SELECT * FROM hybrid_search(
            :query_text,
            :query_embedding::vector,
            :match_count
        )
        LIMIT :limit OFFSET :offset
    """)

    count_query = text("""
        SELECT COUNT(*) FROM hybrid_search(
            :query_text,
            :query_embedding::vector,
            :match_count
        )
    """)

    total = await db.scalar(
        count_query,
        {"query_text": q, "query_embedding": vector_str, "match_count": 200}
    )

    result = await db.execute(
        hybrid_query,
        {
            "query_text": q,
            "query_embedding": vector_str,
            "match_count": 200,
            "limit": pageSize,
            "offset": offset,
        }
    )

    rows = result.mappings().all()
    opportunities = [Opportunity(**dict(row)) for row in rows]

    # DS-02 — join cluster context to results
    cluster_ids = list({o.cluster_id for o in opportunities if o.cluster_id is not None})
    cluster_map = {}
    if cluster_ids:
        clusters_result = await db.execute(
            select(Cluster).where(Cluster.id.in_(cluster_ids))
        )
        clusters = clusters_result.scalars().all()

        # Get latest snapshot post_count for each cluster
        snapshots_result = await db.execute(
            select(ClusterSnapshot)
            .where(ClusterSnapshot.cluster_id.in_(cluster_ids))
            .distinct(ClusterSnapshot.cluster_id)
            .order_by(ClusterSnapshot.cluster_id, ClusterSnapshot.date.desc())
        )
        snapshots = snapshots_result.scalars().all()
        snapshot_map = {s.cluster_id: s.post_count for s in snapshots}

        cluster_map = {
            c.id: {
                "id": c.id,
                "name": c.name,
                "postCount": snapshot_map.get(c.id),
                "signalScore": None,  # RD-08
                "trending": None,     # RD-06
            }
            for c in clusters
        }

    db.add(SearchUsage(
        uid=uid,
        query=q,
        result_count=total or 0,
        searched_at=datetime.now(timezone.utc),
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

    if body.action == "add":
        opportunity.pipeline_count = opportunity.pipeline_count + 1
    elif body.action == "remove":
        opportunity.pipeline_count = max(0, opportunity.pipeline_count - 1)
    elif body.action == "launch":
        opportunity.pipeline_count = max(0, opportunity.pipeline_count - 1)
        opportunity.launch_count = opportunity.launch_count + 1
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

    return {
        "success": True,
        "pipelineCount": opportunity.pipeline_count,
        "launchCount": opportunity.launch_count,
    }