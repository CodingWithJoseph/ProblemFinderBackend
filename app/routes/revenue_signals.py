from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db import get_db
from app.auth import get_uid
from app.models import RevenueSignal

router = APIRouter(prefix="/revenue-signals", tags=["revenue_signals"])


def _f(v):
    return float(v) if v is not None else None


def _serialize(r: RevenueSignal) -> dict:
    return {
        "id": r.id,
        "userId": r.user_id,
        "clusterId": r.cluster_id,
        "opportunityId": r.opportunity_id,
        "pipelineId": r.pipeline_id,
        "domain": r.domain,
        "businessModel": r.business_model,
        "offerType": r.offer_type,
        "pricePoint": _f(r.price_point),
        "revenueAmount": _f(r.revenue_amount),
        "revenuePeriod": r.revenue_period,
        "conversionCount": r.conversion_count,
        "customerCount": r.customer_count,
        "sourceChannel": r.source_channel,
        "attributionConfidence": r.attribution_confidence,
        "reportedAt": r.reported_at.isoformat() if r.reported_at else None,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


class CreateRevenueSignalBody(BaseModel):
    cluster_id: Optional[int] = None
    opportunity_id: Optional[str] = None
    pipeline_id: Optional[str] = None
    domain: Optional[str] = None
    business_model: Optional[str] = None
    offer_type: Optional[str] = None
    price_point: Optional[float] = None
    revenue_amount: Optional[float] = None
    revenue_period: Optional[str] = None
    conversion_count: Optional[int] = None
    customer_count: Optional[int] = None
    source_channel: Optional[str] = None
    attribution_confidence: Optional[float] = None
    reported_at: Optional[datetime] = None


class PatchRevenueSignalBody(BaseModel):
    domain: Optional[str] = None
    business_model: Optional[str] = None
    offer_type: Optional[str] = None
    price_point: Optional[float] = None
    revenue_amount: Optional[float] = None
    revenue_period: Optional[str] = None
    conversion_count: Optional[int] = None
    customer_count: Optional[int] = None
    source_channel: Optional[str] = None
    attribution_confidence: Optional[float] = None
    reported_at: Optional[datetime] = None


async def _get_owned(signal_id: str, uid: str, db: AsyncSession) -> RevenueSignal:
    result = await db.execute(
        select(RevenueSignal).where(RevenueSignal.id == signal_id, RevenueSignal.user_id == uid)
    )
    signal = result.scalar_one_or_none()
    if not signal:
        raise HTTPException(status_code=404, detail="Revenue signal not found")
    return signal


@router.get("")
async def list_revenue_signals(
    cluster_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    query = select(RevenueSignal).where(RevenueSignal.user_id == uid)
    if cluster_id is not None:
        query = query.where(RevenueSignal.cluster_id == cluster_id)
    query = query.order_by(RevenueSignal.created_at.desc())
    result = await db.execute(query)
    return {"data": [_serialize(r) for r in result.scalars().all()]}


@router.get("/{signal_id}")
async def get_revenue_signal(
    signal_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    return {"data": _serialize(await _get_owned(signal_id, uid, db))}


@router.post("")
async def create_revenue_signal(
    body: CreateRevenueSignalBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    signal = RevenueSignal(user_id=uid, **body.model_dump(exclude_none=True))
    db.add(signal)
    await db.commit()
    await db.refresh(signal)
    return {"data": _serialize(signal)}


@router.patch("/{signal_id}")
async def patch_revenue_signal(
    signal_id: str,
    body: PatchRevenueSignalBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    signal = await _get_owned(signal_id, uid, db)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(signal, field, value)
    await db.commit()
    await db.refresh(signal)
    return {"data": _serialize(signal)}


@router.delete("/{signal_id}")
async def delete_revenue_signal(
    signal_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    signal = await _get_owned(signal_id, uid, db)
    await db.delete(signal)
    await db.commit()
    return {"success": True}
