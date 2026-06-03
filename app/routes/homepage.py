from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.config import PIPELINE_VERSION
from app.models import Cluster, Opportunity, SourcePost

router = APIRouter(prefix="/homepage", tags=["homepage"])


@router.get("/stats")
async def homepage_stats(db: AsyncSession = Depends(get_db)):
    """Public aggregate counts for the marketing homepage."""
    source_post_count = await db.scalar(
        select(func.count())
        .select_from(SourcePost)
        .where(SourcePost.pipeline_version == PIPELINE_VERSION)
    )
    opportunity_count = await db.scalar(
        select(func.count()).where(
            Opportunity.deleted_at == None,
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
    )
    cluster_count = await db.scalar(
        select(func.count())
        .select_from(Cluster)
        .where(Cluster.pipeline_version == PIPELINE_VERSION)
    )
    high_signal_count = await db.scalar(
        select(func.count()).where(
            Opportunity.deleted_at == None,
            Opportunity.signal_score >= 50,
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
    )
    wtp_signal_count = await db.scalar(
        select(func.count()).where(
            Opportunity.deleted_at == None,
            Opportunity.wtp_signal == True,
            Opportunity.pipeline_version == PIPELINE_VERSION,
        )
    )

    opportunities_found = opportunity_count or 0

    return {
        "conversationsAnalyzed": source_post_count or opportunities_found,
        "clustersDetected": cluster_count or 0,
        "opportunitiesFound": opportunities_found,
        "highSignalOpportunities": high_signal_count or 0,
        "wtpSignals": wtp_signal_count or 0,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
