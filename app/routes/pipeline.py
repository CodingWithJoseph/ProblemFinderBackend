from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db import get_db
from app.auth import get_uid
from app.models import Pipeline, Opportunity

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

PATCHABLE_FIELDS = {
    "stage", "kill_criteria", "distribution_channels", "concept_angles",
    "exit_checklist", "notes", "url", "category", "revenue_model", "status", "name",
}

CAMEL_TO_SNAKE = {
    "killCriteria": "kill_criteria",
    "distributionChannels": "distribution_channels",
    "conceptAngles": "concept_angles",
    "exitChecklist": "exit_checklist",
    "revenueModel": "revenue_model",
    "stage": "stage",
    "notes": "notes",
    "url": "url",
    "category": "category",
    "status": "status",
    "name": "name",
}


def _serialize(p: Pipeline) -> dict:
    return {
        "id": p.id,
        "userId": p.user_id,
        "name": p.name,
        "postIds": p.post_ids or [],
        "sourceClusterId": p.source_cluster_id,
        "stage": p.stage,
        "killCriteria": p.kill_criteria,
        "distributionChannels": p.distribution_channels or [],
        "conceptAngles": p.concept_angles or [],
        "exitChecklist": p.exit_checklist,
        "notes": p.notes,
        "url": p.url,
        "category": p.category,
        "revenueModel": p.revenue_model,
        "status": p.status,
        "launchedAt": p.launched_at.isoformat() if p.launched_at else None,
        "removedAt": p.removed_at.isoformat() if p.removed_at else None,
        "createdAt": p.created_at.isoformat() if p.created_at else None,
        "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
    }


async def _update_opportunity_counts(opportunity_id: str, action: str, db: AsyncSession) -> None:
    result = await db.execute(select(Opportunity).where(Opportunity.id == opportunity_id))
    opp = result.scalar_one_or_none()
    if not opp:
        return
    pipeline_count = opp.pipeline_count or 0
    launch_count = opp.launch_count or 0
    if action == "add":
        opp.pipeline_count = pipeline_count + 1
    elif action == "remove":
        opp.pipeline_count = max(0, pipeline_count - 1)
    elif action == "launch":
        opp.pipeline_count = max(0, pipeline_count - 1)
        opp.launch_count = launch_count + 1


class CreatePipelineBody(BaseModel):
    name: str
    post_ids: list[str]
    source_cluster_id: Optional[str] = None


class PatchPipelineBody(BaseModel):
    stage: Optional[str] = None
    kill_criteria: Optional[str] = None
    distribution_channels: Optional[list[str]] = None
    concept_angles: Optional[list] = None
    exit_checklist: Optional[dict] = None
    notes: Optional[str] = None
    url: Optional[str] = None
    category: Optional[str] = None
    revenue_model: Optional[str] = None
    status: Optional[str] = None
    name: Optional[str] = None


class LaunchBody(BaseModel):
    product_name: str


@router.get("")
async def get_pipeline(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.launched_at == None,
            Pipeline.removed_at == None,
        ).order_by(Pipeline.created_at.desc())
    )
    cards = result.scalars().all()
    return {"data": [_serialize(c) for c in cards]}


@router.post("")
async def create_pipeline(
    body: CreatePipelineBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    if not body.post_ids:
        raise HTTPException(status_code=400, detail="post_ids must not be empty")

    # Duplicate check: any active cluster already contains one of these post_ids
    existing_result = await db.execute(
        select(Pipeline).where(
            Pipeline.user_id == uid,
            Pipeline.launched_at == None,
            Pipeline.removed_at == None,
        )
    )
    active_cards = existing_result.scalars().all()
    new_ids_set = set(body.post_ids)
    for card in active_cards:
        if new_ids_set.intersection(card.post_ids or []):
            raise HTTPException(
                status_code=409,
                detail={"error": "DUPLICATE_POST_IDS", "existingClusterId": card.id},
            )

    card = Pipeline(
        user_id=uid,
        name=body.name,
        post_ids=body.post_ids,
        source_cluster_id=body.source_cluster_id,
    )
    db.add(card)
    await db.flush()  # get the generated id

    for post_id in body.post_ids:
        await _update_opportunity_counts(post_id, "add", db)

    await db.commit()
    await db.refresh(card)
    return {"data": _serialize(card)}


@router.patch("/{pipeline_id}")
async def patch_pipeline(
    pipeline_id: str,
    body: PatchPipelineBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    updates = body.model_dump(exclude_none=True)
    for field, value in updates.items():
        setattr(card, field, value)
    card.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(card)
    return {"data": _serialize(card)}


@router.post("/{pipeline_id}/launch")
async def launch_pipeline(
    pipeline_id: str,
    body: LaunchBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")
    if card.launched_at:
        raise HTTPException(status_code=409, detail="Already launched")

    card.launched_at = datetime.now(timezone.utc)
    card.name = body.product_name
    card.updated_at = datetime.now(timezone.utc)

    for post_id in card.post_ids or []:
        await _update_opportunity_counts(post_id, "launch", db)

    await db.commit()
    await db.refresh(card)
    return {"data": _serialize(card)}


@router.delete("/{pipeline_id}")
async def delete_pipeline(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.user_id == uid)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    card.removed_at = datetime.now(timezone.utc)
    card.updated_at = datetime.now(timezone.utc)

    for post_id in card.post_ids or []:
        await _update_opportunity_counts(post_id, "remove", db)

    await db.commit()
    return {"success": True}


