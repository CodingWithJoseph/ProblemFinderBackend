from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.auth import get_uid
from app.models import Opportunity, Subscription, PlanEnum

router = APIRouter(prefix="/opportunities", tags=["feed"])


@router.get("")
async def get_opportunities(
        page: int = Query(1, ge=1),
        pageSize: int = Query(default=20, ge=1, le=100),
        category: str | None = Query(None),
        sort: str | None = Query(None),
        db: AsyncSession = Depends(get_db),
        uid: str = Depends(get_uid),
):
    query = select(Opportunity).where(Opportunity.deleted_at == None)

    sub_result = await db.execute(
        select(Subscription).where(Subscription.uid == uid)
    )
    sub = sub_result.scalar_one_or_none()
    plan = sub.plan if sub else PlanEnum.free

    if plan == PlanEnum.free:
        page = min(page, 1)
        pageSize = min(pageSize, 50)

    offset = (page - 1) * pageSize

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
        "data": [
            {
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
            for o in opportunities
        ],
        "page": page,
        "pageSize": pageSize,
        "total": total,
    }
