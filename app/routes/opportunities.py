from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, cast, Date
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from app.db import get_db
from app.auth import get_uid
from app.models import Opportunity, SearchUsage, Subscription, PlanEnum

router = APIRouter(prefix="/opportunities", tags=["opportunities"])


def _serialize(o: Opportunity) -> dict:
    return {
        "id": o.id,
        "title": o.title,
        "description": o.description,
        "source": o.source,
        "sourceUrl": o.source_url,
        "category": o.category,
        "score": o.score,
        "upvotes": o.upvotes,
        "trending": o.trending,
        "wtpSignal": o.wtp_signal,
        "workaround": o.workaround,
        "intensity": o.intensity,
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
    category: str | None = Query(None),
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

    if category:
        query = query.where(Opportunity.category == category)

    if sort == "trending":
        query = query.where(Opportunity.trending == True)
    elif sort == "score":
        query = query.order_by(Opportunity.score.desc())
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

    offset = (page - 1) * pageSize

    query = (
        select(Opportunity)
        .where(
            Opportunity.deleted_at == None,
            Opportunity.search_vector.op("@@")(
                func.plainto_tsquery("english", q)
            )
        )
        .order_by(
            func.ts_rank(
                Opportunity.search_vector,
                func.plainto_tsquery("english", q)
            ).desc()
        )
    )

    count_query = select(func.count()).select_from(query.subquery())
    total = await db.scalar(count_query)

    query = query.offset(offset).limit(pageSize)
    result = await db.execute(query)
    opportunities = result.scalars().all()

    db.add(SearchUsage(
        uid=uid,
        query=q,
        result_count=total or 0,
        searched_at=datetime.now(timezone.utc),
    ))

    return {
        "data": [_serialize(o) for o in opportunities],
        "page": page,
        "pageSize": pageSize,
        "total": total,
    }


class PipelineActionRequest(BaseModel):
    action: str  # "add" | "remove" | "launch"


@router.post("/{opportunity_id}/pipeline-action")
async def pipeline_action(
    opportunity_id: int,
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