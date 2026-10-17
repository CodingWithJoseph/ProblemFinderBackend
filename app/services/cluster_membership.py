from typing import Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline


async def load_watched_cluster_ids(
        uid: str,
        cluster_ids: list[int],
        db: AsyncSession
) -> Set[int]:
    if not cluster_ids:
        return set()

    rows = (
        await db.execute(
            select(
                Pipeline.source_cluster_id
            ).where(
                Pipeline.user_id == uid,
                Pipeline.source_cluster_id.in_(
                    [str(cluster_id) for cluster_id in cluster_ids]
                ),
                Pipeline.removed_at.is_(None)
            )
        )
    ).all()

    watched: set[int] = set()
    for (source_cluster_id,) in rows:
        try:
            watched.add(int(source_cluster_id))
        except (TypeError, ValueError):
            continue

    return watched