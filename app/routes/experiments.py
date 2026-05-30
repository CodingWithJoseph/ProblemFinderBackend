from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db import get_db
from app.auth import get_uid
from app.models import Experiment, ExperimentResult

router = APIRouter(prefix="/experiments", tags=["experiments"])


def _f(v):
    return float(v) if v is not None else None


def _serialize_result(r: ExperimentResult) -> dict:
    return {
        "id": r.id,
        "experimentId": r.experiment_id,
        "impressions": r.impressions,
        "clicks": r.clicks,
        "signups": r.signups,
        "replies": r.replies,
        "purchases": r.purchases,
        "conversionRate": r.conversion_rate,
        "revenueGenerated": _f(r.revenue_generated),
        "cost": _f(r.cost),
        "notes": r.notes,
        "resultSummary": r.result_summary,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


def _serialize(e: Experiment, results: list[ExperimentResult] | None = None) -> dict:
    return {
        "id": e.id,
        "userId": e.user_id,
        "clusterId": e.cluster_id,
        "opportunityId": e.opportunity_id,
        "pipelineId": e.pipeline_id,
        "experimentName": e.experiment_name,
        "hypothesis": e.hypothesis,
        "targetAudience": e.target_audience,
        "offer": e.offer,
        "channel": e.channel,
        "status": e.status,
        "startedAt": e.started_at.isoformat() if e.started_at else None,
        "endedAt": e.ended_at.isoformat() if e.ended_at else None,
        "createdAt": e.created_at.isoformat() if e.created_at else None,
        "results": [_serialize_result(r) for r in (results or [])],
    }


class CreateExperimentBody(BaseModel):
    experiment_name: Optional[str] = None
    hypothesis: Optional[str] = None
    target_audience: Optional[str] = None
    offer: Optional[str] = None
    channel: Optional[str] = None
    status: Optional[str] = None
    cluster_id: Optional[int] = None
    opportunity_id: Optional[str] = None
    pipeline_id: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class PatchExperimentBody(BaseModel):
    experiment_name: Optional[str] = None
    hypothesis: Optional[str] = None
    target_audience: Optional[str] = None
    offer: Optional[str] = None
    channel: Optional[str] = None
    status: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class CreateResultBody(BaseModel):
    impressions: Optional[int] = None
    clicks: Optional[int] = None
    signups: Optional[int] = None
    replies: Optional[int] = None
    purchases: Optional[int] = None
    conversion_rate: Optional[float] = None
    revenue_generated: Optional[float] = None
    cost: Optional[float] = None
    notes: Optional[str] = None
    result_summary: Optional[str] = None


async def _get_owned(experiment_id: str, uid: str, db: AsyncSession) -> Experiment:
    result = await db.execute(
        select(Experiment).where(Experiment.id == experiment_id, Experiment.user_id == uid)
    )
    experiment = result.scalar_one_or_none()
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return experiment


async def _results_for(experiment_id: str, db: AsyncSession) -> list[ExperimentResult]:
    res = await db.execute(
        select(ExperimentResult)
        .where(ExperimentResult.experiment_id == experiment_id)
        .order_by(ExperimentResult.created_at.asc())
    )
    return list(res.scalars().all())


@router.get("")
async def list_experiments(
    pipeline_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    query = select(Experiment).where(Experiment.user_id == uid)
    if pipeline_id:
        query = query.where(Experiment.pipeline_id == pipeline_id)
    query = query.order_by(Experiment.created_at.desc())
    experiments = list((await db.execute(query)).scalars().all())
    return {"data": [_serialize(e, await _results_for(e.id, db)) for e in experiments]}


@router.get("/{experiment_id}")
async def get_experiment(
    experiment_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    experiment = await _get_owned(experiment_id, uid, db)
    return {"data": _serialize(experiment, await _results_for(experiment_id, db))}


@router.post("")
async def create_experiment(
    body: CreateExperimentBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    experiment = Experiment(user_id=uid, **body.model_dump(exclude_none=True))
    db.add(experiment)
    await db.commit()
    await db.refresh(experiment)
    return {"data": _serialize(experiment, [])}


@router.patch("/{experiment_id}")
async def patch_experiment(
    experiment_id: str,
    body: PatchExperimentBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    experiment = await _get_owned(experiment_id, uid, db)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(experiment, field, value)
    await db.commit()
    await db.refresh(experiment)
    return {"data": _serialize(experiment, await _results_for(experiment_id, db))}


@router.delete("/{experiment_id}")
async def delete_experiment(
    experiment_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    experiment = await _get_owned(experiment_id, uid, db)
    await db.delete(experiment)
    await db.commit()
    return {"success": True}


@router.get("/{experiment_id}/results")
async def list_results(
    experiment_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _get_owned(experiment_id, uid, db)
    return {"data": [_serialize_result(r) for r in await _results_for(experiment_id, db)]}


@router.post("/{experiment_id}/results")
async def add_result(
    experiment_id: str,
    body: CreateResultBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    await _get_owned(experiment_id, uid, db)
    result = ExperimentResult(experiment_id=experiment_id, **body.model_dump(exclude_none=True))
    db.add(result)
    await db.commit()
    await db.refresh(result)
    return {"data": _serialize_result(result)}
