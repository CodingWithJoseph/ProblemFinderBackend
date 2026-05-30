from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.auth import get_uid
from app.models import DomainSnapshot

router = APIRouter(prefix="/domain-snapshots", tags=["domain_snapshots"])


def _f(v):
    return float(v) if v is not None else None


def _serialize(s: DomainSnapshot) -> dict:
    return {
        "id": s.id,
        "domain": s.domain,
        "snapshotDate": s.snapshot_date.isoformat() if s.snapshot_date else None,
        "clusterCount": s.cluster_count,
        "opportunityCount": s.opportunity_count,
        "avgSignalScore": s.avg_signal_score,
        "avgGrowthRate": s.avg_growth_rate,
        "avgRevenuePotentialScore": s.avg_revenue_potential_score,
        "avgCommercialIntentScore": s.avg_commercial_intent_score,
        "avgPainScore": s.avg_pain_score,
        "totalReportedRevenue": _f(s.total_reported_revenue),
        "avgConversionRate": s.avg_conversion_rate,
        "topClusters": s.top_clusters,
        "createdAt": s.created_at.isoformat() if s.created_at else None,
    }


@router.get("")
async def list_domain_trends(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Most recent snapshot per domain (for the dashboard domain-trend cards)."""
    result = await db.execute(
        select(DomainSnapshot)
        .distinct(DomainSnapshot.domain)
        .order_by(DomainSnapshot.domain, DomainSnapshot.snapshot_date.desc())
    )
    rows = sorted(
        result.scalars().all(),
        key=lambda s: (s.avg_signal_score or 0.0),
        reverse=True,
    )
    return {"data": [_serialize(s) for s in rows]}


@router.get("/{domain}")
async def domain_timeseries(
    domain: str,
    limit: int = Query(90, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Full daily time series for one domain (oldest -> newest)."""
    result = await db.execute(
        select(DomainSnapshot)
        .where(DomainSnapshot.domain == domain)
        .order_by(DomainSnapshot.snapshot_date.desc())
        .limit(limit)
    )
    rows = list(reversed(result.scalars().all()))
    return {"domain": domain, "data": [_serialize(s) for s in rows]}
