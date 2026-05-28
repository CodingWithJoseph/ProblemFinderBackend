from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.auth import get_uid
from app.models import Pipeline
from app.routes.pipeline import _serialize

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("")
async def get_portfolio(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.launched_at != None,
        ).order_by(Pipeline.launched_at.desc())
    )
    cards = result.scalars().all()
    return {"data": [_serialize(c) for c in cards]}
