"""Released Pipeline project endpoints."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_uid
from app.config import PIPELINE_VERSION
from app.db import get_db
from app.models import (
    Cluster,
    ClusterItem,
    Pipeline,
    SignalAnalysisCase,
    SolutionCase,
)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

PIPELINE_STAGES = ("watching", "discovery", "exploring", "validating", "building")
VALID_STAGES = frozenset(PIPELINE_STAGES)


def _serialize(
    card: Pipeline,
    signal_case: SignalAnalysisCase | None = None,
    solution_case: SolutionCase | None = None,
) -> dict:
    return {
        "id": card.id,
        "userId": card.user_id,
        "name": card.name,
        "projectName": card.project_name,
        "displayName": card.project_name or card.name,
        "postIds": card.post_ids or [],
        "sourceClusterId": card.source_cluster_id,
        "stage": card.stage,
        "analysisStatus": signal_case.status if signal_case else "not_started",
        "buildStatus": solution_case.status if solution_case else "not_started",
        "approvalStatus": (
            solution_case.approval_status if solution_case else "not_started"
        ),
        "launchedAt": card.launched_at.isoformat() if card.launched_at else None,
        "removedAt": card.removed_at.isoformat() if card.removed_at else None,
        "createdAt": card.created_at.isoformat() if card.created_at else None,
        "updatedAt": card.updated_at.isoformat() if card.updated_at else None,
    }


async def _load_workflow_cases(
    cards: list[Pipeline],
    db: AsyncSession,
    uid: str,
) -> tuple[dict[str, SignalAnalysisCase], dict[str, SolutionCase]]:
    pipeline_ids = [str(card.id) for card in cards]
    if not pipeline_ids:
        return {}, {}

    signal_cases = (
        await db.execute(
            select(SignalAnalysisCase).where(
                SignalAnalysisCase.pipeline_id.in_(pipeline_ids),
                SignalAnalysisCase.user_id == uid,
            )
        )
    ).scalars().all()
    solution_cases = (
        await db.execute(
            select(SolutionCase).where(
                SolutionCase.pipeline_id.in_(pipeline_ids),
                SolutionCase.user_id == uid,
            )
        )
    ).scalars().all()
    return (
        {str(case.pipeline_id): case for case in signal_cases},
        {str(case.pipeline_id): case for case in solution_cases},
    )


class PatchPipelineBody(BaseModel):
    stage: str | None = None
    project_name: str | None = None


class LaunchBody(BaseModel):
    product_name: str


class WatchBody(BaseModel):
    cluster_id: str


@router.get("")
async def get_pipeline(
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Pipeline)
        .where(Pipeline.user_id == uid, Pipeline.removed_at == None)
        .order_by(Pipeline.created_at.desc())
    )
    cards = list(result.scalars().all())
    signal_cases, solution_cases = await _load_workflow_cases(cards, db, uid)
    return {
        "data": [
            _serialize(
                card,
                signal_cases.get(str(card.id)),
                solution_cases.get(str(card.id)),
            )
            for card in cards
        ]
    }


@router.get("/{pipeline_id}")
async def get_pipeline_card(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = (
        await db.execute(
            select(Pipeline).where(
                Pipeline.id == pipeline_id,
                Pipeline.user_id == uid,
                Pipeline.removed_at == None,
            )
        )
    ).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")
    signal_cases, solution_cases = await _load_workflow_cases([card], db, uid)
    return {
        "data": _serialize(
            card,
            signal_cases.get(str(card.id)),
            solution_cases.get(str(card.id)),
        )
    }


async def _get_pipeline_card(
    pipeline_id: str,
    db: AsyncSession,
    uid: str,
) -> Pipeline:
    """Resolve an active or launched project for the released Signal workspace."""
    card = (
        await db.execute(
            select(Pipeline).where(
                Pipeline.id == pipeline_id,
                Pipeline.user_id == uid,
                Pipeline.removed_at == None,
            )
        )
    ).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")
    return card


async def _find_watch_card(
    uid: str,
    cluster_id: str,
    db: AsyncSession,
) -> Pipeline | None:
    return (
        await db.execute(
            select(Pipeline).where(
                Pipeline.user_id == uid,
                Pipeline.source_cluster_id == str(cluster_id),
                Pipeline.removed_at == None,
            )
        )
    ).scalar_one_or_none()


@router.post("/watch")
async def watch_cluster(
    body: WatchBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    """Create a project without spending analysis capacity."""
    existing = await _find_watch_card(uid, body.cluster_id, db)
    if existing:
        return {"is_watched": True, "pipeline_id": str(existing.id)}

    try:
        cluster_id = int(body.cluster_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Cluster not found")

    cluster = (
        await db.execute(
            select(Cluster).where(
                Cluster.id == cluster_id,
                Cluster.pipeline_version == PIPELINE_VERSION,
            )
        )
    ).scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    post_id_rows = (
        await db.execute(
            select(ClusterItem.id).where(
                ClusterItem.cluster_id == cluster_id,
                ClusterItem.pipeline_version == PIPELINE_VERSION,
            )
        )
    ).all()
    card = Pipeline(
        user_id=uid,
        name=cluster.name or f"Cluster {body.cluster_id}",
        post_ids=[str(row[0]) for row in post_id_rows],
        source_cluster_id=str(body.cluster_id),
        stage="watching",
    )
    db.add(card)
    await db.commit()
    await db.refresh(card)
    return {"is_watched": True, "pipeline_id": str(card.id)}


@router.delete("/watch/{cluster_id}")
async def unwatch_cluster(
    cluster_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = await _find_watch_card(uid, cluster_id, db)
    if card:
        now = datetime.now(timezone.utc)
        card.removed_at = now
        card.updated_at = now
        await db.commit()
    return {"is_watched": False}


@router.patch("/{pipeline_id}")
async def patch_pipeline(
    pipeline_id: str,
    body: PatchPipelineBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = (
        await db.execute(
            select(Pipeline).where(
                Pipeline.id == pipeline_id,
                Pipeline.user_id == uid,
                Pipeline.removed_at == None,
            )
        )
    ).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    updates = body.model_dump(exclude_none=True)
    if "stage" in updates and updates["stage"] not in VALID_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid stage '{updates['stage']}'. Must be one of: {', '.join(PIPELINE_STAGES)}",
        )

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
    card = (
        await db.execute(
            select(Pipeline).where(
                Pipeline.id == pipeline_id,
                Pipeline.user_id == uid,
                Pipeline.removed_at == None,
            )
        )
    ).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")
    if card.launched_at:
        raise HTTPException(status_code=409, detail="Already launched")

    now = datetime.now(timezone.utc)
    card.launched_at = now
    card.name = body.product_name
    card.updated_at = now
    await db.commit()
    await db.refresh(card)
    return {"data": _serialize(card)}


@router.delete("/{pipeline_id}")
async def delete_pipeline(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    card = (
        await db.execute(
            select(Pipeline).where(
                Pipeline.id == pipeline_id,
                Pipeline.user_id == uid,
                Pipeline.removed_at == None,
            )
        )
    ).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Pipeline card not found")

    now = datetime.now(timezone.utc)
    card.removed_at = now
    card.updated_at = now
    await db.commit()
    return {"ok": True}
