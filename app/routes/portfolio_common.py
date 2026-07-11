"""Shared portfolio product guards.

These small ownership checks are used by several portfolio route modules
(the main portfolio router, the goals router, and revenue routes), so they live
here to avoid one route module importing another just for a lookup.
"""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline


async def require_launched_product(pipeline_id: str, db: AsyncSession, uid: str) -> Pipeline:
    product = (await db.execute(
        select(Pipeline).where(
            Pipeline.id == pipeline_id,
            Pipeline.user_id == uid,
            Pipeline.launched_at != None,
        )
    )).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Launched product not found")
    return product


async def require_owned_product(pipeline_id: str, db: AsyncSession, uid: str) -> Pipeline:
    """The caller's product in any launch state (used where pre-launch access is
    valid, e.g. journey goals)."""
    product = (await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product
