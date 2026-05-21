from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.db import get_db
from app.models import Opportunity, SearchUsage, Subscription, PlanEnum

router = APIRouter(prefix="/opportunities", tags=["search"])


@router.get("/search")
async def search_opportunities(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    from datetime import datetime, timezone
    from sqlalchemy import select, func, cast, Date

    if True:
        sub_result = await db.execute(
            select(Subscription).where(Subscription.uid == uid)
        )
        sub = sub_result.scalar_one_or_none()
        plan = sub.plan if sub else PlanEnum.free

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

    # log search usage
    db.add(SearchUsage(
        uid=uid,
        query=q,
        result_count=total or 0,
        searched_at=datetime.now(timezone.utc),
    ))

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
            }
            for o in opportunities
        ],
        "page": page,
        "pageSize": pageSize,
        "total": total,
    }