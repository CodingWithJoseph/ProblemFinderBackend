from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.auth import get_uid
from app.models import Pipeline
from app.routes.pipeline import (
    _fetch_issue_counts_for_cards,
    _fetch_posts_for_cards,
    _fetch_teams_for_cards,
    _serialize_cards,
)

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
    posts_map = await _fetch_posts_for_cards(list(cards), db)
    teams_map = await _fetch_teams_for_cards(list(cards), db)
    counts_map = await _fetch_issue_counts_for_cards(list(cards), db)
    return {"data": _serialize_cards(list(cards), posts_map, teams_map, counts_map)}
