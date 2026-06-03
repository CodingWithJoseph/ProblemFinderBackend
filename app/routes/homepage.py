from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.routes.public import compute_stats

router = APIRouter(prefix="/homepage", tags=["homepage"])


@router.get("/stats")
async def homepage_stats(db: AsyncSession = Depends(get_db)):
    """Backwards-compatible alias for GET /public/stats.

    The canonical implementation now lives in app/routes/public.py
    (BACKEND JOB 2); this endpoint is preserved so existing clients keep
    working.
    """
    return await compute_stats(db)
