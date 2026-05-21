from app.db import get_db
from app.auth import get_uid
from pydantic import BaseModel
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import PipelineCard, Opportunity, StageEnum
from fastapi import APIRouter, Depends, HTTPException, status

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class PipelineCreateRequest(BaseModel):
    opportunityId: int
    stage: StageEnum = StageEnum.watching


class PipelineUpdateRequest(BaseModel):
    stage: StageEnum | None = None
    killCriteria: dict | None = None
    distributionChannels: dict | None = None
    conceptAngles: dict | None = None
    exitChecklist: dict | None = None
    action: str | None = None  # "launch" | "remove"


@router.get("")
async def get_pipeline(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(PipelineCard).where(
            PipelineCard.uid == uid,
            PipelineCard.removed_at == None,
        )
    )
    cards = result.scalars().all()
    return {"data": [_serialize(c) for c in cards]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_to_pipeline(
    body: PipelineCreateRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = PipelineCard(
        uid=uid,
        opportunity_id=body.opportunityId,
        stage=body.stage,
    )
    db.add(card)
    await db.execute(
        update(Opportunity)
        .where(Opportunity.id == body.opportunityId)
        .values(pipeline_count=Opportunity.pipeline_count + 1)
    )
    await db.flush()
    await db.refresh(card)
    return _serialize(card)


@router.patch("/{card_id}")
async def update_pipeline_card(
    card_id: int,
    body: PipelineUpdateRequest,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_card(card_id, uid, db)

    if body.stage:
        card.stage = body.stage
    if body.killCriteria is not None:
        card.kill_criteria = body.killCriteria
    if body.distributionChannels is not None:
        card.distribution_channels = body.distributionChannels
    if body.conceptAngles is not None:
        card.concept_angles = body.conceptAngles
    if body.exitChecklist is not None:
        card.exit_checklist = body.exitChecklist
    if body.action == "launch":
        card.launched_at = datetime.now(timezone.utc)
        await db.execute(
            update(Opportunity)
            .where(Opportunity.id == card.opportunity_id)
            .values(launch_count=Opportunity.launch_count + 1)
        )
    if body.action == "remove":
        card.removed_at = datetime.now(timezone.utc)

    return _serialize(card)


@router.delete("/{card_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pipeline_card(
    card_id: int,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _get_card(card_id, uid, db)
    card.removed_at = datetime.now(timezone.utc)


async def _get_card(card_id: int, uid: str, db: AsyncSession) -> PipelineCard:
    result = await db.execute(
        select(PipelineCard).where(
            PipelineCard.id == card_id,
            PipelineCard.uid == uid,
        )
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    return card


def _serialize(c: PipelineCard) -> dict:
    return {
        "id": c.id,
        "uid": c.uid,
        "opportunityId": c.opportunity_id,
        "stage": c.stage,
        "killCriteria": c.kill_criteria,
        "distributionChannels": c.distribution_channels,
        "conceptAngles": c.concept_angles,
        "exitChecklist": c.exit_checklist,
        "launchedAt": c.launched_at,
        "removedAt": c.removed_at,
    }