from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db import get_db
from app.auth import get_uid
from app.models import Problem

router = APIRouter(prefix="/problems", tags=["problems"])


def _serialize(p: Problem) -> dict:
    return {
        "id": p.id,
        "pipelineId": p.pipeline_id,
        "userId": p.user_id,
        "title": p.title,
        "description": p.description,
        "sourcePostId": p.source_post_id,
        "position": p.position,
        "createdAt": p.created_at.isoformat() if p.created_at else None,
        "updatedAt": p.updated_at.isoformat() if p.updated_at else None,
    }


class CreateProblemBody(BaseModel):
    pipeline_id: str
    title: str
    description: Optional[str] = None
    source_post_id: Optional[str] = None


class PatchProblemBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    position: Optional[int] = None


@router.get("")
async def list_problems(
    pipeline_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Problem)
        .where(Problem.pipeline_id == pipeline_id, Problem.user_id == uid)
        .order_by(Problem.position.asc(), Problem.created_at.asc())
    )
    problems = result.scalars().all()
    return {"data": [_serialize(p) for p in problems]}


@router.post("")
async def create_problem(
    body: CreateProblemBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    problem = Problem(
        pipeline_id=body.pipeline_id,
        user_id=uid,
        title=body.title,
        description=body.description,
        source_post_id=body.source_post_id,
    )
    db.add(problem)
    await db.commit()
    await db.refresh(problem)
    return {"data": _serialize(problem)}


@router.patch("/{problem_id}")
async def patch_problem(
    problem_id: str,
    body: PatchProblemBody,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Problem).where(Problem.id == problem_id, Problem.user_id == uid)
    )
    problem = result.scalar_one_or_none()
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")

    updates = body.model_dump(exclude_none=True)
    for field, value in updates.items():
        setattr(problem, field, value)
    problem.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(problem)
    return {"data": _serialize(problem)}


@router.delete("/{problem_id}")
async def delete_problem(
    problem_id: str,
    db: AsyncSession = Depends(get_db),
    uid: str = Depends(get_uid),
):
    result = await db.execute(
        select(Problem).where(Problem.id == problem_id, Problem.user_id == uid)
    )
    problem = result.scalar_one_or_none()
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")

    await db.delete(problem)
    await db.commit()
    return {"success": True}
